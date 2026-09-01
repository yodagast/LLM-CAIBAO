"""港股低价选股服务: 全市场扫描港股, 筛选接近 52 周低点的公司。

策略逻辑 (对应选股 tab「低价选股」港股模式, 与 A 股 low_price_service 口径一致):
  - 每只港股取腾讯日线 (约 8 年, 不复权), 聚合成**月线**计算 52 周最低价/最高价
    (最近 12 个月月线 low 最小值 / high 最大值), 与 A 股月线口径一致。
  - 条件: 最近收盘价 >= 52 周最低价 (成立前提), 且 (最近收盘价 - 52周最低价) /
    52周最低价 <= 最大偏离阈值。
  - 附带财务 (东财主要指标: roe / gross_margin / pe_ttm / pb / total_mv) 供筛选展示。

数据流 (2026-09-01):
  - 定时任务 (sync_hk_low_price.py) 每日计算全市场结果入库 hk_low_price_screen;
  - 接口 `/api/hk/lowprice/screen`: **优先从 pg 读当天+该行业数据**; 若无当天或无该行业数据,
    则按该行业 (或全市场) 实时计算并自动入库 (兜底)。

数据源:
  - tushare hk_basic         全部港股列表 (hk_data_service.hk_stock_list)
  - 东财 datacenter          港股行业映射 + 主要财务指标 (ROE/毛利率/PE/PB/总市值)
  - 腾讯港股 K 线             日线 (52周高低 + 最近收盘价, 月线聚合)
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd

from . import data_service as ds
from . import hk_data_service as hkd
from . import pg_service as pg


async def _candidates(industry: str = "") -> pd.DataFrame:
    """全部港股候选 (剔除 -R 柜台股); industry 非空时按行业子串过滤。

    返回 DataFrame 列: ts_code / name / market (行业由 industry_map 单独补充)。
    """
    stocks = await hkd.hk_stock_list()
    ind_map = await hkd.industry_map()
    rows: list[dict] = []
    for _, r in stocks.iterrows():
        ts_code = str(r["ts_code"])
        name = str(r.get("name") or "")
        if name.rstrip().endswith("-R"):
            continue  # RMB 柜台股, 与主柜重复
        ind = ind_map.get(ts_code, "")
        if industry.strip() and industry.strip() not in ind:
            continue
        rows.append({
            "ts_code": ts_code,
            "symbol": ts_code.split(".")[0],
            "name": name,
            "market": str(r.get("market") or ""),
            "industry": ind,
        })
    return pd.DataFrame(rows).reset_index(drop=True)


async def _calc_date() -> str:
    """计算日: 港股最新交易日 (以 00700.HK 腾讯 K 线最新日期为锚, 周末/节假日返回上一交易日)。"""
    try:
        df = await hkd._tencent_kline_df("00700.HK")
        if not df.empty:
            return df["date"].iloc[-1].strftime("%Y%m%d")
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")


def _hk_monthly_52week(df: pd.DataFrame) -> tuple[float, float] | None:
    """港股日线 df (date/open/close/high/low/vol) → 月线聚合 52 周高低 (与 A 股口径一致)。

    把 date 转成 trade_date (YYYYMMDD 字符串) 后复用 data_service._monthly_52week_high_low。
    """
    if df is None or df.empty or "date" not in df.columns:
        return None
    d = df.copy().sort_values("date")
    d["trade_date"] = d["date"].dt.strftime("%Y%m%d")
    return ds._monthly_52week_high_low(d)


async def hk_calc_low_price(max_dev_pct: float = 15.0, industry: str = "") -> list[dict]:
    """按腾讯日线(月线聚合)计算接近 52 周低点的港股 (不含入库), 返回全部命中 (按偏离度升序)。

    industry 非空时只计算该行业 (子串匹配); 每项含: ts_code / symbol / name / industry /
    close / week52_high / week52_low / dev_pct / pct_chg / pe_ttm / pb / total_mv /
    roe / gross_margin。
    """
    cand = await _candidates(industry)
    if cand.empty:
        return []
    stocks_by_code = {str(r["ts_code"]): r for _, r in cand.iterrows()}
    codes = list(stocks_by_code.keys())

    # 行业映射 (每只候选的行业)
    # (candidates 已带 industry, 无需再查)

    # 并行拉取: 腾讯日线 + 东财财务指标 (每只 2 次请求, K 线进程内缓存)
    sem = asyncio.Semaphore(8)
    fina_cache: dict[str, dict[int, dict]] = {}

    async def _one(code: str) -> dict | None:
        async with sem:
            try:
                row = stocks_by_code[code]
                df = await hkd._tencent_kline_df(code)
                if df.empty:
                    return None
                monthly = _hk_monthly_52week(df)
                if monthly is None:
                    return None
                w52_high, w52_low = monthly
                last_close = float(df["close"].iloc[-1])
                closes = df["close"].dropna()
                prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else None
                pct_chg = None
                if prev_close and prev_close > 0:
                    pct_chg = (last_close / prev_close - 1.0) * 100.0
                if w52_low is None or w52_low <= 0 or last_close is None:
                    return None
                dev_pct = (last_close - w52_low) / w52_low * 100.0

                # 东财财务指标 (最新财年: 最大 year 的年报)
                fina = fina_cache.get(code)
                if fina is None:
                    try:
                        fina = await hkd._fina_indicator_map(code)
                        fina_cache[code] = fina
                    except Exception:
                        fina = {}
                years = [y for y in fina.keys() if isinstance(fina[y], dict)]
                fy = fina[max(years)] if years else {}
                return {
                    "ts_code": code,
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "market": row["market"],
                    "industry": row["industry"],
                    "close": last_close,
                    "week52_high": w52_high,
                    "week52_low": w52_low,
                    "dev_pct": round(dev_pct, 2),
                    "pct_chg": round(pct_chg, 2) if pct_chg is not None else None,
                    "pe_ttm": fy.get("pe_ttm"),
                    "pb": fy.get("pb"),
                    "total_mv": fy.get("total_mv_wan"),
                    "roe": fy.get("roe"),
                    "gross_margin": fy.get("gross_margin"),
                }
            except Exception:
                return None

    tasks = [_one(c) for c in codes]
    results = await asyncio.gather(*tasks) if tasks else []
    out: list[dict] = []
    for snap in results:
        if snap is None:
            continue
        if snap["close"] >= snap["week52_low"] and snap["dev_pct"] <= max_dev_pct:
            out.append(snap)
    out.sort(key=lambda x: x["dev_pct"])
    return out


async def hk_sync_low_price_to_db(max_dev_pct: float = 15.0) -> int:
    """计算全市场港股低价选股结果并写入 hk_low_price_screen 表 (定时任务入口)。

    入库全部结果 (不按行业过滤), calc_date 用最新交易日锚点; 返回写入行数。
    """
    items = await hk_calc_low_price(max_dev_pct=max_dev_pct)
    if not items:
        return 0
    calc_date = await _calc_date()
    return await _store_items(items, calc_date, max_dev_pct)


async def hk_get_low_price(industry: str = "", max_dev_pct: float = 15.0,
                           filters: dict | None = None,
                           allow_live: bool = True) -> tuple[list[dict], bool, str]:
    """港股低价选股入口 (读库优先, 兜底实时计算)。

    **要求**:
      1. 存在 **当天 + 该行业** 的 pgsql 数据 → 直接从 pg 拉取 (source=db, 秒回);
      2. 无当天或无该行业数据 → 按该行业 (空=全市场) 实时计算并自动入库 (source=live)。

    filters: ROE/毛利率 阈值筛选 (与红利低波一致)。

    返回 (items, from_db, calc_date)。
    """
    calc_date = await _calc_date()
    # 1) 读库优先: 精确判断 当天 + 该行业 的数据是否存在
    try:
        hit = await pg.has_hk_low_price_data(calc_date, industry)
        if hit > 0:
            rows = await pg.query_hk_low_price(calc_date=calc_date, industry=industry,
                                               max_dev_pct=max_dev_pct, limit=10000,
                                               filters=filters)
            return rows, True, calc_date
    except Exception:
        pass

    # 2) 无当天/该行业数据: 实时计算并入库
    if not allow_live:
        return [], False, calc_date
    items = await hk_calc_low_price(max_dev_pct=max_dev_pct, industry=industry)
    if filters:
        items = _apply_filters(items, filters)
    try:
        await _store_items(items, calc_date, max_dev_pct)
    except Exception:
        pass
    return items, False, calc_date


def _apply_filters(items: list[dict], filters: dict) -> list[dict]:
    """对低价选股结果应用 ROE/毛利率阈值筛选 (与 A 股 low_price_service 一致)。"""
    out: list[dict] = []
    for it in items:
        ok = True
        for key, flt in (filters or {}).items():
            if key not in ("roe", "gross_margin"):
                continue
            if isinstance(flt, dict):
                mn, mx = flt.get("min"), flt.get("max")
            else:
                mn, mx = flt, None
            v = it.get(key)
            if v is None:
                ok = False
                break
            if mn is not None and v < mn:
                ok = False
                break
            if mx is not None and v > mx:
                ok = False
                break
        if ok:
            out.append(it)
    return out


async def _store_items(items: list[dict], calc_date: str, max_dev_pct: float) -> int:
    """把计算结果 upsert 入库。"""
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
            "roe": it.get("roe"),
            "gross_margin": it.get("gross_margin"),
            "max_dev_pct": max_dev_pct,
        })
    return await pg.upsert_hk_low_price_rows(rows)