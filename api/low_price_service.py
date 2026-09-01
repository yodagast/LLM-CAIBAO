"""低价选股服务: 全市场扫描 A 股, 筛选接近 52 周低点的公司。

策略逻辑 (对应选股 tab「低价选股」):
  - 每只 A 股取**最近约 24 个月 k 线 (月线)**, 计算 52 周最低价 (week52_low) 与 最近收盘价 (close)。
    月线数据量小 (每月 1 行, 24 行/股), 全市场扫描远快于日线 (250 行/股);
    tushare 月线 monthly 返回 开/高/低/收, 52 周最低 ≈ 最近 12 个月的低点。
  - 条件: 最近收盘价 >= 52 周最低价 (成立前提), 且 (最近收盘价 - 52周最低价) / 52周最低价 <= 最大偏离阈值。
  - 阈值 max_dev_pct (默认 15%) 控制"离 52 周低点多近"; 越接近低点偏离越小。
  - 附带估值 (daily_basic: pe_ttm / pb / total_mv) 与涨跌幅, 供结果表展示与排序。

数据流 (2026-09-01):
  - 定时任务 (sync_low_price.py) 每日计算全市场结果入库 low_price_screen;
  - 接口 `/api/lowprice/screen`: **优先从 pg 读当天+该行业数据**; 若无当天或无该行业数据,
    则用 **tushare 月线** 按该行业 (或全市场) 实时计算并自动入库 (兜底)。

数据源 (tushare):
  - pro.stock_basic            全部 A 股
  - pro.monthly                近 24 个月月线 (52周最高/最低 + 最近收盘价, 月线 low/high)
  - pro.daily_basic            最新估值 (pe_ttm / pb / total_mv)
  - 批量接口优先: pro.daily(trade_date=最新) 一次全市场取最近收盘价。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pandas as pd

from . import data_service as ds
from . import pg_service as pg


async def _candidates(industry: str = "") -> pd.DataFrame:
    """全部上市 A 股 (已过滤基金/ETF); industry 非空时按行业子串过滤。"""
    stocks = await ds._stock_basic()
    if industry.strip():
        stocks = stocks[stocks["industry"].str.contains(industry.strip(), na=False)]
    return stocks.reset_index(drop=True)


async def _calc_date() -> str:
    """计算日: 17 点前用前一交易日, 17 点后用当天 (与行情展示规则一致的 display_td)。"""
    try:
        pro = ds._init_pro()
        _, display_td = await ds._display_trade_dates(pro)
        if display_td:
            return display_td
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")


async def calc_low_price(max_dev_pct: float = 15.0, industry: str = "") -> list[dict]:
    """按 tushare 月线计算接近 52 周低点的公司 (不含入库), 返回全部命中 (按偏离度升序)。

    industry 非空时只计算该行业 (子串匹配); 每项含: ts_code / symbol / name / industry /
    close / week52_high / week52_low / dev_pct / pct_chg / pe_ttm / pb / total_mv。
    """
    stocks = await _candidates(industry)
    cand = stocks.head(6000)

    pro = ds._init_pro()
    # 全市场最新交易日收盘 (一次批量, 保证"最近收盘价"与展示规则一致)
    display_td = ""
    try:
        _, display_td = await ds._display_trade_dates(pro)
        latest_daily = await pro.daily(trade_date=display_td, fields="ts_code,close,pct_chg")
    except Exception:
        latest_daily = None
    if latest_daily is None or latest_daily.empty:
        dmap: dict[str, dict] = {}
    else:
        dmap = {}
        for _, r in latest_daily.iterrows():
            dmap[str(r["ts_code"])] = {
                "close": ds._to_float(r.get("close")),
                "pct_chg": ds._to_float(r.get("pct_chg")),
            }

    # 最新估值 (一次批量)
    try:
        basic_map = await ds._latest_basic_map(pro)
    except Exception:
        basic_map = {}

    # 批量月线: 逐月请求月末 trade_date (每月返回全市场该月月线), 聚合每只股票最近 12 个月高低点。
    # 仅 12 次全市场请求, 避免逐只拉月线触发 tushare monthly 限频(300次/分钟)。
    now = datetime.now()
    month_rows: dict[str, list[dict]] = {}  # ts_code -> [{trade_date, close, high, low}]
    for m in range(11, -1, -1):
        # 计算该月月末日期 (下一月首日 - 1 天)
        yr, mo = now.year, now.month
        base = (yr, mo - m)
        y2 = base[0] + (base[1] - 1) // 12
        m2 = (base[1] - 1) % 12 + 1
        if m2 == 12:
            nxt = f"{y2 + 1}0101"
        else:
            nxt = f"{y2}{m2 + 1:02d}01"
        end_dt = datetime.strptime(nxt, "%Y%m%d") - timedelta(days=1)
        td = end_dt.strftime("%Y%m%d")
        try:
            df = await pro.monthly(trade_date=td, fields="ts_code,trade_date,close,high,low")
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                code = str(r["ts_code"])
                month_rows.setdefault(code, []).append({
                    "trade_date": str(r["trade_date"]),
                    "close": ds._to_float(r.get("close")),
                    "high": ds._to_float(r.get("high")),
                    "low": ds._to_float(r.get("low")),
                })
        except Exception:
            continue  # 单月失败跳过 (月份少不影响最近月份)

    sem = asyncio.Semaphore(12)

    async def _one(code: str, symbol: str, name: str, ind: str, months: list[dict]) -> dict | None:
        async with sem:
            try:
                recent = sorted([m for m in months if m.get("low") is not None and m.get("high") is not None],
                                key=lambda x: x["trade_date"])
                recent = recent[-12:]
                if not recent:
                    return None
                lows: list[float] = [float(x["low"]) for x in recent if x.get("low") is not None]
                highs: list[float] = [float(x["high"]) for x in recent if x.get("high") is not None]
                if not lows:
                    return None
                w52_low: float = min(lows)
                w52_high: float = max(highs) if highs else w52_low
                closes: list[float] = [float(x["close"]) for x in recent if x.get("close") is not None]
                last_close = closes[-1] if closes else None
                d = dmap.get(code) or {}
                if d.get("close") is not None:
                    close = float(d["close"])
                elif last_close is not None:
                    close = float(last_close)
                else:
                    return None
                if w52_low is None or w52_low <= 0 or close is None:
                    return None
                # 核心条件: 52周最低 <= 最近收盘 (成立前提) 且 偏离度在阈值内
                dev_pct = (close - w52_low) / w52_low * 100.0
                b = basic_map.get(code) or {}
                return {
                    "ts_code": code,
                    "symbol": symbol,
                    "name": name,
                    "industry": ind,
                    "close": close,
                    "week52_high": w52_high,
                    "week52_low": w52_low,
                    "dev_pct": round(dev_pct, 2),
                    "pct_chg": d.get("pct_chg"),
                    "pe_ttm": b.get("pe_ttm"),
                    "pb": b.get("pb"),
                    "total_mv": b.get("total_mv"),
                }
            except Exception:
                return None

    # 只并发处理候选股票 (行业过滤后); 用批量月线聚合数据
    tasks = []
    for _, r in cand.iterrows():
        code = r["ts_code"]
        months = month_rows.get(code, [])
        if not months:
            continue
        tasks.append(_one(code, r["symbol"], r["name"], str(r.get("industry") or ""), months))
    results = await asyncio.gather(*tasks) if tasks else []
    out: list[dict] = []
    for snap in results:
        if snap is None:
            continue
        # 筛选: 最近收盘价 >= 52周最低, 且偏离度 <= 阈值
        if snap["close"] >= snap["week52_low"] and snap["dev_pct"] <= max_dev_pct:
            out.append(snap)

    out.sort(key=lambda x: x["dev_pct"])
    return out


async def sync_low_price_to_db(max_dev_pct: float = 15.0) -> int:
    """计算全市场低价选股结果并写入 low_price_screen 表 (定时任务入口)。

    入库全部结果 (不按行业过滤), calc_date 用当日 display_td; 返回写入行数。
    """
    items = await calc_low_price(max_dev_pct=max_dev_pct)
    if not items:
        return 0
    calc_date = await _calc_date()
    return await _store_items(items, calc_date, max_dev_pct)


async def get_low_price(industry: str = "", max_dev_pct: float = 15.0,
                        allow_live: bool = True) -> tuple[list[dict], bool, str]:
    """低价选股入口 (读库优先, 兜底月线计算)。

    **要求**:
      1. 存在 **当天 + 该行业** 的 pgsql 数据 → 直接从 pg 拉取 (source=db, 秒回);
      2. 无当天或无该行业数据 → 用 **tushare 月线** 按该行业 (空=全市场) 实时计算并自动入库
         (source=live)。

    返回 (items, from_db, calc_date): from_db=True 数据来自 pg; False 为实时月线计算兜底
    (结果亦已入库供下次读库); calc_date 为计算日 (YYYYMMDD)。
    """
    calc_date = await _calc_date()
    # 1) 读库优先: 精确判断 当天 + 该行业 的数据是否存在 (行业空=全市场判断)
    try:
        hit = await pg.has_low_price_data(calc_date, industry)
        if hit > 0:
            rows = await pg.query_low_price(calc_date=calc_date, industry=industry,
                                            max_dev_pct=max_dev_pct, limit=10000)
            return rows, True, calc_date
    except Exception:
        pass

    # 2) 无当天/该行业数据: 用 tushare 月线按该行业 (空=全市场) 实时计算并入库
    if not allow_live:
        return [], False, calc_date
    items = await calc_low_price(max_dev_pct=max_dev_pct, industry=industry)
    # 入库本次结果 (行业查询只入该行业结果, 供同行业下次读库; 全市场入全市场)
    try:
        await _store_items(items, calc_date, max_dev_pct)
    except Exception:
        pass
    return items, False, calc_date


async def _store_items(items: list[dict], calc_date: str, max_dev_pct: float) -> int:
    """把计算结果 upsert 入库 (供 _store_items 直接调用/测试)。"""
    rows = []
    for it in items:
        rows.append({
            "calc_date": calc_date,
            "ts_code": it["ts_code"],
            "symbol": it.get("symbol", ""),
            "name": it.get("name", ""),
            "industry": it.get("industry", ""),
            "close": it["close"],
            "week52_high": it["week52_high"],
            "week52_low": it["week52_low"],
            "dev_pct": it["dev_pct"],
            "pct_chg": it.get("pct_chg"),
            "pe_ttm": it.get("pe_ttm"),
            "pb": it.get("pb"),
            "total_mv": it.get("total_mv"),
            "max_dev_pct": max_dev_pct,
        })
    return await pg.upsert_low_price_rows(rows)