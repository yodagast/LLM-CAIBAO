"""每日公司推荐服务: 全市场区间交易参数估算 → 入库 daily_band_recommend → 筛选推荐。

工作流:
  scan_all: 批量计算所有/指定公司的最优 买入/卖出/止损价 与指标 (复用 band_service.optimize_band,
            前复权 + T+1 + 剔除无效交易 + max_trades 限制), 按 (calc_date, ts_code) 入库,
            并筛选「买入价 ≥ 当日收盘价」的公司作为每日推荐。
  get_recommendations: 从 pgsql 查询最近计算日的推荐列表。

calc_date = 数据最新交易日 (前复权价格序列最后一行 trade_date)。
可指定回测起始/结束日期 (start_date/end_date) 控制估算所用历史区间。
"""

from __future__ import annotations

import time

from . import band_service, data_service, pg_service


def _all_stocks(limit: int = 0) -> list:
    """全市场沪深 A 股列表 [(ts_code, name)]。"""
    df = data_service._stock_basic()
    df = df[df["ts_code"].astype(str).str.endswith((".SH", ".SZ"))]
    df = df.sort_values("ts_code")
    if limit and limit > 0:
        df = df.head(limit)
    return [(str(r["ts_code"]), str(r["name"])) for _, r in df.iterrows()]


def _industry_stocks(industry: str, limit: int = 0) -> list:
    """按东财行业子串匹配获取股票列表。"""
    df = data_service._stock_basic()
    df = df[df["industry"].fillna("").astype(str).str.contains(industry, na=False)]
    df = df.sort_values("ts_code")
    if limit and limit > 0:
        df = df.head(limit)
    return [(str(r["ts_code"]), str(r["name"])) for _, r in df.iterrows()]


def _stock_industry_map() -> dict:
    """ts_code → 东财行业 映射 (用于入库时标注行业)。"""
    df = data_service._stock_basic()
    return {str(r["ts_code"]): str(r.get("industry") or "").strip()
            for _, r in df.iterrows()}


def _backfill_industry() -> None:
    """回填 daily_band_recommend 中行业为空的历史行 (幂等)。"""
    pg_service.init_daily_rec_schema()
    m = _stock_industry_map()
    pg_service.backfill_daily_rec_industry(m)


def _probe_trade_date() -> str:
    """探测最新交易日 (用高流动性股票, 仅用于缓存命中判断)。"""
    for code in ("000001.SZ", "600000.SH"):
        try:
            df = data_service.get_daily(code, "stock", "20250101", "", adj="qfq")
            if df is not None and not df.empty:
                return str(df["trade_date"].iloc[-1])
        except Exception:
            continue
    return ""


def scan_all(codes: str = "", industry: str = "", limit: int = 0,
             objective: str = "balanced", min_sharpe: float = 1.0,
             max_trades: int | None = 100, sleep: float = 0.0,
             start_date: str = "20170101", end_date: str = "",
             use_cache: bool = True, skip_existing: bool = False,
             batch: int = 0) -> dict:
    """批量估算区间交易参数并入库, 返回筛选结果 (buy_price >= close)。

    codes: 逗号分隔代码 (空则用 industry 或全市场); limit: 0=不限(全市场约5200只, 耗时数小时)。
    start_date/end_date: 回测历史区间 (YYYYMMDD, end 空=最新)。
    use_cache: 行业扫描时若 pgsql 已有当天(calc_date)+该行业数据则直接读取, 不重复计算。
    skip_existing: 续跑模式, 跳过该 calc_date 已入库的标的 (中断后可续跑)。
    batch: >0 时分批 upsert 入库 (每 batch 只一次), 中断时已入库数据不丢。
    """
    pg_service.init_daily_rec_schema()
    # 缓存命中: 行业扫描 + 默认区间(20170101~最新) + pgsql 已有当天该行业数据 → 直接读库
    if use_cache and industry and not codes and start_date == "20170101" and not end_date:
        cur = _probe_trade_date()
        if cur:
            existing = pg_service.has_daily_rec(cur, industry)
            if existing:
                print(f"✓ 缓存命中: {cur} 行业[{industry}] 已有 {existing} 行, 直接读取")
                recs = get_recommendations(cur, limit=2000, industry=industry)
                recs.update({
                    "cached": True,
                    "scanned": existing,
                    "ok": existing,
                    "fail": 0,
                    "stored": existing,
                    "recommend_count": recs["count"],
                    "start_date": start_date,
                    "end_date": end_date or "",
                })
                return recs
    # 股票列表
    if codes:
        stocks = [(c.strip(), c.strip()) for c in codes.split(",") if c.strip()]
    elif industry:
        stocks = _industry_stocks(industry, limit)
    else:
        stocks = _all_stocks(limit)
    ind_map = _stock_industry_map()  # 总是构建: 全市场扫描也写入行业

    # 续跑: 跳过该 calc_date 已入库的标的
    skipped = 0
    if skip_existing:
        cur = _probe_trade_date()
        if cur:
            done = set(pg_service.daily_rec_done_codes(cur))
            before = len(stocks)
            stocks = [s for s in stocks if s[0] not in done]
            skipped = before - len(stocks)
            if skipped:
                print(f"续跑: 该计算日已入库 {len(done)} 只, 跳过 {skipped} 只, 待计算 {len(stocks)} 只")

    rows: list[dict] = []
    recommends: list[dict] = []
    calc_date = None
    ok = fail = 0
    stored = 0
    for i, (ts_code, _n) in enumerate(stocks, 1):
        try:
            info = data_service.resolve_code(ts_code)
            df = data_service.get_daily(info["ts_code"], kind=info["kind"],
                                        start_date=start_date, end_date=end_date, adj="qfq")
            if df is None or df.empty:
                fail += 1
                continue
            cur_date = str(df["trade_date"].iloc[-1])
            close = float(df["close"].iloc[-1])
            r = band_service.optimize_band(df, capital=100000, min_sharpe=min_sharpe,
                                           objective=objective, max_trades=max_trades)
            p, m, s = r["params"], r["band"]["metrics"], r["search"]
            rows.append({
                "calc_date": cur_date,
                "ts_code": info["ts_code"],
                "name": info.get("name", ts_code),
                "kind": info.get("kind", "stock"),
                "close": round(close, 4),
                "buy_price": p["buy_price"],
                "sell_price": p["sell_price"],
                "stop_price": p["stop_price"],
                "total_return": round(m["total_return"], 2),
                "annual_return": round(m["annual_return"], 2),
                "max_drawdown": round(m["max_drawdown"], 2),
                "sharpe": round(m["sharpe"], 2),
                "calmar": round(m["calmar"], 2),
                "trades": len(r["trades"]),
                "objective": objective,
                "industry": ind_map.get(info["ts_code"], ""),
                "achieved": bool(s["achieved"]),
            })
            if calc_date is None:
                calc_date = cur_date
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  ✗ {ts_code} 估算失败: {e}")
        if sleep and sleep > 0:
            time.sleep(sleep)
        # 分块入库: 每 batch 只 flush 一次, 中断不丢已算数据
        if batch and batch > 0 and len(rows) >= batch:
            stored += pg_service.upsert_daily_rec_rows(rows)
            for r in rows:
                if r["buy_price"] is not None and r["close"] is not None \
                        and r["buy_price"] >= r["close"]:
                    recommends.append(r)
            rows = []
        if i % 200 == 0:
            print(f"  已处理 {i}/{len(stocks)} ...")

    stored += pg_service.upsert_daily_rec_rows(rows) if rows else 0
    # 筛选: 买入价 >= 当日收盘价 (不低于)
    for r in rows:
        if r["buy_price"] is not None and r["close"] is not None \
                and r["buy_price"] >= r["close"]:
            recommends.append(r)
    return {
        "calc_date": calc_date,
        "scanned": len(stocks),
        "ok": ok,
        "fail": fail,
        "stored": stored,
        "skipped": skipped,
        "start_date": start_date,
        "end_date": end_date or "",
        "recommend_count": len(recommends),
        "items": recommends,
    }


def get_recommendations(calc_date: str = "", limit: int = 500, industry: str = "") -> dict:
    """从 pgsql 查询最近计算日的推荐列表 (buy_price >= close, 按收盘价降序)。

    industry 非空时仅返回该行业 (子串匹配) 的标的。
    """
    _backfill_industry()
    rows = pg_service.query_daily_recommend(calc_date or None, buy_above_close=True,
                                            limit=limit, industry=industry)
    date = rows[0]["calc_date"] if rows else pg_service.latest_calc_date()
    return {"calc_date": date, "count": len(rows), "items": rows, "industry": industry}


def recommend_dividend(min_dy_ttm: float = 3.0, industry: str = "",
                       year_min: int | None = None, year_max: int | None = None,
                       limit: int = 500,
                       payout_min: float | None = None, payout_max: float | None = None,
                       roe_min: float | None = None, roe_max: float | None = None) -> dict:
    """方法2: 按红利低波选股, 推荐 动态股息率(股息率-TTM) >= N 的公司。

    直接读 pgsql 的 red_low_vol (无需重算), 按股息率-TTM 降序。
    year_min/year_max: 年份区间 (均空→最新单年); payout/roe 可选范围。
    """
    items = pg_service.query_dividend_recommend(
        min_dy_ttm=min_dy_ttm, industry=industry, year_min=year_min, year_max=year_max,
        limit=limit, payout_min=payout_min, payout_max=payout_max,
        roe_min=roe_min, roe_max=roe_max)
    latest = None
    if not year_min and not year_max:
        latest = pg_service.latest_rlv_year()
    calc = year_max or year_min or latest
    return {
        "method": "dividend",
        "min_dy_ttm": min_dy_ttm,
        "year_min": year_min,
        "year_max": year_max,
        "calc_date": str(calc) if calc else "",
        "industry": industry,
        "payout_min": payout_min,
        "payout_max": payout_max,
        "roe_min": roe_min,
        "roe_max": roe_max,
        "count": len(items),
        "items": items,
    }
