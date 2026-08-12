"""精选策略 Hub: 预置市场常用选股策略 + 执行筛选 + 回测参考指标 (缓存)。

策略复用现有 PG 表 (red_low_vol / fundamental_screen / etf_screen / hk_red_low_vol),
筛选逻辑映射到 pg_service 现有查询函数; 回测用当前入选股票池近 N 年等权持有表现
(含幸存者偏差, 仅作策略参考指标, 不构成投资建议)。
"""
import time
from datetime import datetime, timedelta

from . import data_service, etf_service, pg_service

BACKTEST_YEARS = 5
_BACKTEST_CACHE: dict[str, dict] = {}   # key -> 回测结果 (缓存 24h)
_CACHE_TTL = 24 * 3600

# ---------------------------------------------------------------------------
# 策略定义
#   type: rlv=A股红利低波 / fund=A股基本面 / etf=ETF / hk_rlv=港股红利低波
#   query 为筛选参数 (见 _query_by_type 的映射)
# ---------------------------------------------------------------------------
STRATEGIES: list[dict] = [
    {
        "key": "high_dividend",
        "name": "高股息红利",
        "category": "红利策略",
        "desc": "股息率(TTM)≥5%、分红率30~80%、年波动率≤25% 的高分红低波动公司",
        "tags": ["高股息", "低波动"],
        "type": "rlv",
        "query": {"min_dy_ttm": 5.0, "payout_min": 30.0, "payout_max": 80.0, "volatility_max": 25.0},
        "sort": "dividend_yield_ttm", "order": "desc", "limit": 20,
    },
    {
        "key": "low_vol_dividend",
        "name": "低波动红利",
        "category": "红利策略",
        "desc": "股息率(TTM)≥3%、年波动率≤15%，红利中波动最小的稳健标的",
        "tags": ["低波动", "高股息"],
        "type": "rlv",
        "query": {"min_dy_ttm": 3.0, "volatility_max": 15.0},
        "sort": "dividend_yield_ttm", "order": "desc", "limit": 20,
    },
    {
        "key": "white_horse",
        "name": "白马蓝筹",
        "category": "质量策略",
        "desc": "ROE≥15%、资产负债率≤50%、毛利率≥30% 的优质蓝筹",
        "tags": ["高质量", "蓝筹"],
        "type": "fund",
        "query": {"roe_min": 15.0, "debt_max": 50.0, "gross_margin_min": 30.0},
        "sort": "roe", "order": "desc", "limit": 20,
    },
    {
        "key": "quality_growth",
        "name": "高质量成长",
        "category": "质量策略",
        "desc": "ROE≥15%、净利率≥15%、总资产周转≥0.5 的盈利能力强成长标的",
        "tags": ["高ROE", "高净利率"],
        "type": "fund",
        "query": {"roe_min": 15.0, "net_margin_min": 15.0, "assets_turn_min": 0.5},
        "sort": "net_margin", "order": "desc", "limit": 20,
    },
    {
        "key": "value_dividend",
        "name": "低估值价值",
        "category": "价值策略",
        "desc": "股息率(TTM)≥4%、ROE≥10%、资产负债率≤60% 的便宜且能分红的价值股",
        "tags": ["低估值", "高股息"],
        "type": "rlv",
        "query": {"min_dy_ttm": 4.0, "roe_min": 10.0, "debt_max": 60.0},
        "sort": "dividend_yield_ttm", "order": "desc", "limit": 20,
    },
    {
        "key": "cash_cow",
        "name": "现金奶牛",
        "category": "价值策略",
        "desc": "自由现金流为正且充沛、股息率(TTM)≥3%、ROE≥10% 的现金创造型公司",
        "tags": ["现金流", "高股息"],
        "type": "rlv",
        "query": {"min_dy_ttm": 3.0, "roe_min": 10.0, "fcff_min": 0.0},
        "sort": "free_cashflow", "order": "desc", "limit": 20,
    },
    {
        "key": "etf_star",
        "name": "ETF 精选",
        "category": "ETF 策略",
        "desc": "规模≥20亿、管理费≤0.5%、折溢价幅度小的大盘优质 ETF",
        "tags": ["宽基", "低费率"],
        "type": "etf",
        "query": {"min_scale": 20.0, "max_m_fee": 0.5, "max_premium": 2.0},
        "sort": "scale", "order": "desc", "limit": 20,
    },
    {
        "key": "hk_dividend",
        "name": "港股高股息",
        "category": "港股策略",
        "desc": "港股股息率(TTM)≥5%、分红率≥30% 的高分红标的",
        "tags": ["港股", "高股息"],
        "type": "hk_rlv",
        "query": {"min_dy_ttm": 5.0, "payout_min": 30.0},
        "sort": "dividend_yield_ttm", "order": "desc", "limit": 20,
    },
]

STRATEGY_MAP: dict[str, dict] = {s["key"]: s for s in STRATEGIES}


def list_strategies() -> list[dict]:
    """返回策略列表 (附缓存回测指标, 若已计算)。"""
    out = []
    for s in STRATEGIES:
        item = dict(s)
        item["query"] = None  # 不下发筛选细节
        item["backtest"] = _cached_backtest(s["key"])
        out.append(item)
    return out


async def run_strategy(key: str, limit: int | None = None) -> dict:
    """执行精选策略, 返回 {strategy, items, meta}。"""
    s = STRATEGY_MAP.get(key)
    if not s:
        raise ValueError(f"未知策略: {key}")
    limit = int(limit or s["limit"] or 20)
    items = await _query_by_type(s, limit)
    meta = {"count": len(items)}
    return {"strategy": {k: v for k, v in s.items() if k not in ("query",)}, "items": items, "meta": meta}


async def _best_year(type_: str) -> int:
    """取数据完整的最近年份 (latest_rlv_year 可能返回残缺年如 2026 仅 72 行)。

    rlv/fund 全市场约 5536 行, 阈值 3000; hk_rlv 全市场约 1000+ 行, 阈值 500。
    """
    table = {"rlv": "red_low_vol", "fund": "fundamental_screen", "hk_rlv": "hk_red_low_vol"}[type_]
    threshold = 500 if type_ == "hk_rlv" else 3000
    pool = await pg_service._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT year, count(*) AS c FROM {table} GROUP BY year ORDER BY year DESC")
    for r in rows:
        if r["c"] >= threshold:
            return int(r["year"])
    return int(rows[0]["year"]) if rows else datetime.now().year - 1


async def _query_by_type(s: dict, limit: int) -> list[dict]:
    t = s["type"]
    q = s["query"]
    if t == "rlv":
        year = await _best_year("rlv")
        filters = {
            "dividend_yield_ttm": {"min": q.get("min_dy_ttm"), "max": q.get("max_dy_ttm")},
            "payout_ratio": {"min": q.get("payout_min"), "max": q.get("payout_max")},
            "volatility": {"max": q.get("volatility_max")},
            "roe": {"min": q.get("roe_min"), "max": q.get("roe_max")},
            "debt_to_assets": {"max": q.get("debt_max")},
            "free_cashflow": {"min": q.get("fcff_min")},
        }
        filters = _clean_filters(filters)
        return await pg_service.query_screen("", [year], sort_by=s["sort"], order=s["order"], limit=limit, filters=filters)
    if t == "fund":
        year = await _best_year("fund")
        filters = {
            "roe": {"min": q.get("roe_min")},
            "debt_to_assets": {"max": q.get("debt_max")},
            "gross_margin": {"min": q.get("gross_margin_min")},
            "net_margin": {"min": q.get("net_margin_min")},
            "assets_turn": {"min": q.get("assets_turn_min")},
        }
        filters = _clean_filters(filters)
        return await pg_service.query_fundamental("", [year], sort_by=s["sort"], order=s["order"], limit=limit, filters=filters)
    if t == "etf":
        # DB 优先, 空库回退实时计算 (全市场首次较慢, 有缓存)
        res = await etf_service.screen_etfs(
            min_scale=q.get("min_scale"), max_m_fee=q.get("max_m_fee"),
            max_c_fee=q.get("max_c_fee"), max_premium=q.get("max_premium"),
            sort_by=s["sort"], order=s["order"], limit=limit)
        return res.get("items", [])
    if t == "hk_rlv":
        year = await _best_year("hk_rlv")
        filters = {
            "dividend_yield_ttm": {"min": q.get("min_dy_ttm"), "max": q.get("max_dy_ttm")},
            "payout_ratio": {"min": q.get("payout_min")},
            "roe": {"min": q.get("roe_min")},
            "debt_to_assets": {"max": q.get("debt_max")},
        }
        filters = _clean_filters(filters)
        return await pg_service.query_hk_rlv("", [year], sort_by=s["sort"], order=s["order"], limit=limit, filters=filters)
    return []


def _clean_filters(filters: dict) -> dict:
    out = {}
    for k, v in filters.items():
        if v is None:
            continue
        mn, mx = v.get("min"), v.get("max")
        if mn is None and mx is None:
            continue
        out[k] = {"min": mn} if mx is None else ({"max": mx} if mn is None else {"min": mn, "max": mx})
    return out


# ---------------------------------------------------------------------------
# 回测参考指标 (缓存 24h)
# ---------------------------------------------------------------------------
def _cached_backtest(key: str) -> dict | None:
    c = _BACKTEST_CACHE.get(key)
    if c and (time.time() - c.get("computed_at", 0)) < _CACHE_TTL:
        return c
    return None


async def backtest_strategy(key: str, years: int = BACKTEST_YEARS) -> dict:
    """对策略当前入选股票池计算近 N 年等权持有参考指标 (累计/年化/最大回撤)。

    含幸存者偏差, 仅作策略参考指标。结果缓存 24h。
    """
    now = time.time()
    cached = _BACKTEST_CACHE.get(key)
    if cached and (now - cached.get("computed_at", 0)) < _CACHE_TTL:
        return cached

    s = STRATEGY_MAP.get(key)
    if not s:
        raise ValueError(f"未知策略: {key}")
    items = await _query_by_type(s, limit=10)
    codes = [it["ts_code"] for it in items]
    result = {"key": key, "status": "ok", "metrics": None, "count": len(codes), "computed_at": int(now)}
    if not codes:
        result["status"] = "no_data"
        _BACKTEST_CACHE[key] = result
        return result

    start = (datetime.now() - timedelta(days=int(years * 365) + 60)).strftime("%Y%m%d")
    annuals, cums, dds = [], [], []
    for code in codes:
        try:
            kind = "hk" if code.endswith(".HK") else ("fund" if data_service._is_fund_code(code) else "stock")
            df = await data_service.get_daily(code, kind=kind, start_date=start, adj="qfq" if kind == "stock" else "")
            if df is None or len(df) < 20:
                continue
            closes = df["close"].tolist()
            first, last = closes[0], closes[-1]
            if not first or first <= 0 or not last:
                continue
            cum = last / first - 1
            n_years = len(closes) / 252.0
            annual = ((1 + cum) ** (1 / n_years) - 1) if (n_years > 0 and (1 + cum) > 0) else None
            peak = closes[0]
            mdd = 0.0
            for c in closes:
                if c > peak:
                    peak = c
                elif peak > 0:
                    mdd = max(mdd, (peak - c) / peak)
            cums.append(cum)
            if annual is not None:
                annuals.append(annual)
            dds.append(mdd)
        except Exception:
            continue

    if cums:
        result["metrics"] = {
            "annual_pct": (sum(annuals) / len(annuals) * 100) if annuals else None,
            "cum_pct": sum(cums) / len(cums) * 100,
            "max_dd_pct": sum(dds) / len(dds) * 100,
        }
    _BACKTEST_CACHE[key] = result
    return result
