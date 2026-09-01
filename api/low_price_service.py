"""低价选股服务: 全市场扫描 A 股, 筛选接近 52 周低点的公司。

策略逻辑 (对应选股 tab「低价选股」):
  - 每只 A 股取最近约 1 年日线 (约 250 交易日), 计算 52 周最低价 (week52_low) 与 最近收盘价 (close)。
  - 条件: 最近收盘价 >= 52 周最低价 (成立前提), 且 (最近收盘价 - 52周最低价) / 52周最低价 <= 最大偏离阈值。
  - 阈值 max_dev_pct (默认 15%) 控制"离 52 周低点多近"; 越接近低点偏离越小。
  - 附带估值 (daily_basic: pe_ttm / pb / total_mv) 与涨跌幅, 供结果表展示与排序。

数据流 (2026-09-01 持久化优化):
  - 定时任务 (nightly_update.sh 新增步骤「低价选股」) 每日计算全市场结果, 入库 low_price_screen 表。
  - 前端接口 `/api/lowprice/screen` 优先从 pg 读当日数据; 无当日数据则实时计算并自动入库 (兜底)。

数据源 (tushare):
  - pro.stock_basic            全部 A 股
  - pro.daily                  近一年日线 (52周最高/最低 + 最近收盘价)
  - pro.daily_basic            最新估值 (pe_ttm / pb / total_mv)
  - 批量接口优先: pro.daily(trade_date=最新) 一次全市场取最近收盘价。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pandas as pd

from . import data_service as ds
from . import pg_service as pg


async def _candidates() -> pd.DataFrame:
    """全部上市 A 股 (已过滤基金/ETF, 只留 stock_basic 里的股票)。"""
    stocks = await ds._stock_basic()
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


async def calc_low_price(max_dev_pct: float = 15.0) -> list[dict]:
    """全市场计算接近 52 周低点的公司 (不含入库), 返回全部命中 (按偏离度升序)。

    每项含: ts_code / symbol / name / industry / close / week52_high / week52_low /
      dev_pct (最近收盘价相对年内低点涨幅 %) / pct_chg / pe_ttm / pb / total_mv。
    """
    stocks = await _candidates()
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

    # 每只股票近一年日线 (并发上限控限频), 计算 52 周高低
    end_date = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    sem = asyncio.Semaphore(10)

    async def _one(code: str, symbol: str, name: str, ind: str) -> dict | None:
        async with sem:
            try:
                df = await ds.get_daily(code, "stock", start_date=start, end_date=end_date)
                if df is None or df.empty:
                    return None
                if display_td:
                    df_t = df[df["trade_date"].astype(str) <= display_td]
                    if not df_t.empty:
                        df = df_t
                recent = df.tail(250)
                if recent.empty:
                    return None
                w52_low = float(recent["low"].min())
                w52_high = float(recent["high"].max())
                d = dmap.get(code) or {}
                if d.get("close") is not None:
                    close = float(d["close"])
                else:
                    last = df.iloc[-1]
                    close = float(last["close"])
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

    results = await asyncio.gather(*(_one(r["ts_code"], r["symbol"], r["name"],
                                          str(r.get("industry") or ""))
                                     for _, r in cand.iterrows()))
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
    """计算全市场低价选股结果并写入 low_price_screen 表 (定时任务/兜底入库用)。

    入库全部结果 (不按行业过滤), calc_date 用当日 display_td; 返回写入行数。
    """
    items = await calc_low_price(max_dev_pct=max_dev_pct)
    if not items:
        return 0
    calc_date = await _calc_date()
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
    stored = await pg.upsert_low_price_rows(rows)
    return stored


async def get_low_price(industry: str = "", max_dev_pct: float = 15.0,
                        allow_live: bool = True) -> tuple[list[dict], bool, str]:
    """低价选股入口: 优先从 pg 读当日数据 (返回全部命中, 不限数量), 无当日则实时计算并自动入库。

    返回 (items, from_db, calc_date): from_db=True 表示数据来自 pg (当日已由定时任务计算),
    False 表示实时计算兜底 (结果亦已入库供下次读库); calc_date 为计算日 (YYYYMMDD)。

    industry/max_dev_pct 过滤在查询层完成; 实时计算时按阈值筛选后入库。
    """
    calc_date = await _calc_date()
    # 1) 尝试从 pg 读当日数据
    try:
        rows = await pg.query_low_price(calc_date=calc_date, industry=industry,
                                        max_dev_pct=max_dev_pct, limit=5000)
        if rows:
            return rows, True, calc_date
    except Exception:
        pass

    # 2) pg 无当日数据: 实时计算 (兜底), 并入库供下次读库
    if not allow_live:
        return [], False, calc_date
    items = await calc_low_price(max_dev_pct=max_dev_pct)
    if industry.strip():
        items = [it for it in items if industry.strip() in (it.get("industry") or "")]
    # 入库本次全市场结果 (幂等 upsert, 供后续读库复用)
    try:
        await sync_low_price_to_db(max_dev_pct=max_dev_pct)
    except Exception:
        pass
    return items, False, calc_date