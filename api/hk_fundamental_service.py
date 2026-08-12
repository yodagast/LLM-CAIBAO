"""港股基本面选股服务: ROE 杜邦拆分指标计算 + 数据同步到 PostgreSQL。

参考 A 股 fundamental_service, 对港股计算并入库:
  年末收盘价、ROE、净利润率、总资产周转率、权益乘数、毛利率、资产负债率、流动比率、
  流动资产、货币资金、存货周转天数、应收账款周转天数、EPS、营收、净利、总市值。

ROE 杜邦拆分: ROE ≈ 净利润率 × 总资产周转率 × 权益乘数
  - 净利润率/毛利率/负债率/权益乘数/流动比率/周转天数 直接来自东财主要财务指标;
  - 总资产周转率 = 营业收入 / 总资产 (与 A 股 assets_turn 口径一致);
  - 流动资产/货币资金 来自东财资产负债表;
  - 年末收盘价/最新收盘 来自腾讯港股日线。

数据来源见 hk_data_service。金额单位为 万港元。
"""

from __future__ import annotations

import asyncio

from . import hk_data_service as hkd
from . import pg_service


async def compute_stock_row(m: dict, year: int) -> dict | None:
    """计算单只港股单年的基本面指标, 返回入库行。"""
    fina = (m.get("fina") or {}).get(year)
    if fina is None:
        return None
    bs = (m.get("balance") or {}).get(year) or {}

    close = await hkd.year_end_close(m["ts_code"], year)

    # 总市值 (万港元): 优先 年末收盘价 × 已发行股本 (逐年); 无K线时退回东财当前市值
    total_mv = None
    shares = fina.get("issued_shares")
    if close and shares:
        total_mv = close * shares / 10000.0
    else:
        total_mv = fina.get("total_mv_wan")

    return {
        "ts_code": m["ts_code"],
        "symbol": m.get("symbol") or m["ts_code"].split(".")[0],
        "name": m.get("name") or "",
        "industry": m.get("industry") or "",
        "market": m.get("market") or "",
        "year": year,
        "close": close,
        "roe": fina.get("roe"),
        "net_margin": fina.get("net_margin"),
        "assets_turn": fina.get("assets_turn"),
        "equity_multiplier": fina.get("equity_multiplier"),
        "gross_margin": fina.get("gross_margin"),
        "debt_to_assets": fina.get("debt_to_assets"),
        "current_ratio": fina.get("current_ratio"),
        "total_cur_assets": bs.get("total_cur_assets_wan"),
        "money_cap": bs.get("money_cap_wan"),
        "invturn_days": fina.get("invturn_days"),
        "arturn_days": fina.get("arturn_days"),
        "eps": fina.get("eps"),
        "operate_income": fina.get("operate_income_wan"),
        "net_profit": fina.get("net_profit_wan"),
        "total_mv": total_mv,
        "end_date": fina.get("end_date") or "",
    }


# ---------------------------------------------------------------------------
# 同步 & 查询
# ---------------------------------------------------------------------------

async def _candidates(industry: str, max_stocks: int) -> list[str]:
    """候选港股 ts_code 列表 (行业子串过滤 + max_stocks 截断, 剔除 -R 柜台股)。"""
    stocks = await hkd.hk_stock_list()
    ind_map = await hkd.industry_map()
    codes: list[str] = []
    for _, row in stocks.iterrows():
        ts_code = str(row["ts_code"])
        name = str(row.get("name") or "")
        if name.rstrip().endswith("-R"):
            continue  # RMB 柜台股, 与主柜重复
        ind = ind_map.get(ts_code, "")
        if industry and industry not in ind:
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
    stored = await pg_service.upsert_hk_fundamental_rows(rows)
    return {"industry": industry, "year": year, "scanned": int(len(codes)),
            "stored": stored, "failed": failed}


async def sync_industry_years(industry: str, years: list[int], max_stocks: int = 3000,
                              sleep: float = 0.1) -> dict:
    total = {"industry": industry, "years": years,
             "stored_total": 0, "scanned_total": 0, "per_year": {}}
    for y in years:
        r = await sync_industry_year(industry, y, max_stocks=max_stocks, sleep=sleep)
        total["stored_total"] += r["stored"]
        total["scanned_total"] += r["scanned"]
        total["per_year"][str(y)] = r
    return total


async def _industry_stock_count(industry: str) -> int | None:
    if not industry:
        return None
    try:
        ind_map = await hkd.industry_map()
        return sum(1 for v in ind_map.values() if industry in v)
    except Exception:
        return None


async def ensure_data(industry: str, years: list[int], max_stocks: int = 3000) -> dict:
    """确保行业+各年份数据已入库; 缺失或记录数远少于行业股票数时自动同步补齐。

    部分港股无东财财务数据, 用 0.8×行业股票数 作为补数阈值, 避免反复全量重同步。
    """
    expected = await _industry_stock_count(industry)
    missing: list[int] = []
    for y in years:
        count = await pg_service.count_hk_fundamental_by_industry_year(industry, y)
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


async def screen(industry: str, years: list[int], sort_by: str = "roe",
                 order: str = "desc", filters: dict | None = None,
                 limit: int = 1000) -> list[dict]:
    return await pg_service.query_hk_fundamental(industry, years, sort_by=sort_by,
                                                 order=order, limit=limit, filters=filters)


# ---------------------------------------------------------------------------
# 正确性校验 (ROE 杜邦拆分一致性)
# ---------------------------------------------------------------------------

def verify_roe(items: list[dict], tol: float = 2.0) -> dict:
    """校验 ROE 拆分: roe ≈ net_margin × assets_turn × equity_multiplier。"""
    checked, passed, bad = 0, 0, []
    for it in items:
        roe = it.get("roe")
        nm = it.get("net_margin")
        at = it.get("assets_turn")
        em = it.get("equity_multiplier")
        if None in (roe, nm, at, em) or at == 0:
            continue
        computed = nm * at * em  # 净利润率% × 周转率 × 权益乘数
        err = abs(computed - roe)
        checked += 1
        if err <= tol:
            passed += 1
        else:
            bad.append({**it, "roe_computed": round(computed, 2), "roe_err": round(err, 2)})
    return {
        "checked": checked,
        "passed": passed,
        "failed": len(bad),
        "bad": bad,
    }
