"""港股红利低波选股服务: 指标计算 + 数据同步到 PostgreSQL。

参考 A 股 redlowvol_service (中证红利低波动指数思路), 对港股计算:
  股息率(静态/TTM)、波动率、每股分红、自由现金流、EPS、分红率、ROE、资产负债率、
  日均市值、日均成交额 等关键指标, 写入 hk_red_low_vol 表。

数据来源 (见 hk_data_service):
  - tushare hk_basic        港股列表
  - 东财 datacenter         财务指标 / 分红 (每股现金分红按财政年度聚合)
  - 腾讯港股 K 线            日线 (波动率/年末收盘/最新收盘/日均成交额)

口径约定:
  - 静态股息率 %   = 当年每股分红 / 当年年末收盘价 × 100
  - 股息率-TTM %   = 最新财政年度每股分红 / 最新收盘价 × 100
  - 波动率 %       = 日收益率 std × sqrt(252) × 100
  - 分红率 %       = 每股分红 / 每股收益 × 100
  - 3年股利增长 %  = (当年分红 / 三年前分红)^(1/3) - 1
  - 金额单位: 万港元 (与 A 股 万元 口径一致)
"""

from __future__ import annotations

import asyncio

from . import hk_data_service as hkd
from . import pg_service


def _dividend_growth_3y(dividends: dict[int, float], year: int) -> float | None:
    """3 年每股股利复合增长率 % = (当年分红 / 三年前分红)^(1/3) - 1。"""
    d0 = dividends.get(year)
    d3 = dividends.get(year - 3)
    if not d0 or not d3 or d0 <= 0 or d3 <= 0:
        return None
    try:
        return (pow(d0 / d3, 1.0 / 3.0) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        return None


async def compute_stock_row(m: dict, year: int, last_close: float | None = None) -> dict | None:
    """计算单只港股单年的红利低波指标, 返回入库行 (缺数据字段为 None)。

    m: hk_data_service.stock_metrics() 返回的原始数据字典。
    """
    fina = (m.get("fina") or {}).get(year)
    if fina is None:
        # 该年无财务数据 (未上市/未披露), 跳过
        return None
    dividends = m.get("dividends") or {}
    balance = m.get("balance") or {}

    year_end_close, volatility = await hkd.year_volatility(m["ts_code"], year)
    if year_end_close is None and fina.get("end_date"):
        # K 线缺失时退回东财总市值/股本推算? 直接置空即可
        pass

    div_per_share = dividends.get(year)
    dividend_growth = _dividend_growth_3y(dividends, year)

    # 静态股息率 = 当年每股分红 / 年末收盘价 × 100
    dividend_yield = None
    if div_per_share is not None and year_end_close:
        dividend_yield = div_per_share / year_end_close * 100.0

    # 股息率-TTM = 最新财政年度每股分红 / 最新收盘价 × 100
    if last_close is None:
        last_close = m.get("last_close")
    dividend_yield_ttm = None
    if div_per_share is not None and last_close:
        dividend_yield_ttm = div_per_share / last_close * 100.0

    # 总市值 (万港元): 优先 年末收盘价 × 已发行股本 (逐年); 无K线时退回东财当前市值
    avg_daily_mv = None
    shares = fina.get("issued_shares")
    if year_end_close and shares:
        avg_daily_mv = year_end_close * shares / 10000.0
    else:
        avg_daily_mv = fina.get("total_mv_wan")

    eps = fina.get("eps")
    payout_ratio = None
    if div_per_share is not None and eps and eps > 0:
        payout_ratio = div_per_share / eps * 100.0

    # 自由现金流 ≈ 经营现金流 + 投资现金流 (万港元); 投资现金流通常为负
    free_cashflow = None
    if fina.get("ocf_wan") is not None and fina.get("icf_wan") is not None:
        free_cashflow = fina["ocf_wan"] + fina["icf_wan"]

    return {
        "ts_code": m["ts_code"],
        "symbol": m.get("symbol") or m["ts_code"].split(".")[0],
        "name": m.get("name") or "",
        "industry": m.get("industry") or "",
        "market": m.get("market") or "",
        "year": year,
        "dividend_yield": dividend_yield,
        "dividend_yield_ttm": dividend_yield_ttm,
        "last_close": last_close,
        "volatility": volatility,
        "div_per_share": div_per_share,
        "free_cashflow": free_cashflow,
        "eps": eps,
        "payout_ratio": payout_ratio,
        "dividend_growth_3y": dividend_growth,
        "roe": fina.get("roe"),
        "debt_to_assets": fina.get("debt_to_assets"),
        "avg_daily_mv": avg_daily_mv,
        "avg_daily_amt": await hkd.avg_daily_amt_wan(m["ts_code"], year),
        "end_date": fina.get("end_date") or "",
    }


# ---------------------------------------------------------------------------
# 同步 & 查询
# ---------------------------------------------------------------------------

async def _candidates(industry: str, max_stocks: int) -> list[str]:
    """候选港股 ts_code 列表 (按行业子串过滤, 可选 max_stocks 截断)。

    剔除 RMB 柜台股 (名称以 -R 结尾, 如 中国移动-R 80941.HK, 与主柜同公司重复,
    且无独立东财财务数据)。
    """
    stocks = await hkd.hk_stock_list()
    ind_map = await hkd.industry_map()
    codes: list[str] = []
    for _, row in stocks.iterrows():
        ts_code = str(row["ts_code"])
        name = str(row.get("name") or "")
        if name.rstrip().endswith("-R"):
            continue  # RMB 柜台股, 与主柜重复
        if industry and industry not in ind_map.get(ts_code, ""):
            continue
        codes.append(ts_code)
    return codes[:max_stocks]


async def sync_industry_year(industry: str, year: int, max_stocks: int = 3000,
                             sleep: float = 0.1) -> dict:
    """对指定行业+年份计算并写入 PG (幂等 upsert)。"""
    codes = await _candidates(industry, max_stocks)
    ind_map = await hkd.industry_map()
    rows = []
    failed = 0
    for ts_code in codes:
        try:
            m = await hkd.stock_metrics(ts_code, ind_map)
            r = await compute_stock_row(m, year)
            if r is None:
                failed += 1
                continue
            rows.append(r)
        except Exception:
            failed += 1
        if sleep > 0:
            await asyncio.sleep(sleep)
    stored = await pg_service.upsert_hk_rlv_rows(rows)
    return {"industry": industry, "year": year, "scanned": int(len(codes)),
            "stored": stored, "failed": failed}


async def sync_industry_years(industry: str, years: list[int], max_stocks: int = 3000,
                              sleep: float = 0.1) -> dict:
    """对指定行业+多个年份逐期同步 (幂等 upsert), 返回各年份统计。"""
    total = {"industry": industry, "years": years,
             "stored_total": 0, "scanned_total": 0, "per_year": {}}
    for y in years:
        r = await sync_industry_year(industry, y, max_stocks=max_stocks, sleep=sleep)
        total["stored_total"] += r["stored"]
        total["scanned_total"] += r["scanned"]
        total["per_year"][str(y)] = r
    return total


async def ensure_data(industry: str, years: list[int], max_stocks: int = 3000) -> dict:
    """确保行业+各年份数据已入库; 缺失或记录数远少于行业股票数时自动同步补齐。

    部分港股无东财财务数据 (新上市/柜台股等), 故用 0.8×行业股票数 作为补数阈值,
    避免个别无数据股票导致每次选股都触发全量重同步。
    """
    expected = await _industry_stock_count(industry)
    missing: list[int] = []
    for y in years:
        count = await pg_service.count_hk_rlv_by_industry_year(industry, y)
        if count == 0 or (expected is not None and count < max(1, int(expected * 0.8))):
            missing.append(y)

    stored_total = 0
    per_year = {}
    for y in missing:
        r = await sync_industry_year(industry, y, max_stocks=max_stocks)
        stored_total += r["stored"]
        per_year[str(y)] = r

    return {"synced": bool(missing), "years": years, "missing": missing,
            "stored": stored_total, "per_year": per_year}


async def _industry_stock_count(industry: str) -> int | None:
    """行业股票总数 (空行业=None, 无法预判)。"""
    if not industry:
        return None
    try:
        ind_map = await hkd.industry_map()
        return sum(1 for v in ind_map.values() if industry in v)
    except Exception:
        return None


async def screen(industry: str, years: list[int], sort_by: str = "dividend_yield",
                 order: str = "desc", filters: dict | None = None,
                 limit: int = 500) -> list[dict]:
    """从 PG 查询该行业+多个年份全部港股, 支持字段阈值筛选, 按指定指标排序。"""
    return await pg_service.query_hk_rlv(industry, years, sort_by=sort_by,
                                         order=order, limit=limit, filters=filters)
