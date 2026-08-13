"""基本面选股服务: ROE 杜邦拆分指标计算 + 数据同步到 PostgreSQL。

对指定行业/年份的每家公司计算并入库:
  最近价格、ROE、净利润率、总资产周转率、权益乘数、毛利率、资产负债率、
  流动资产、货币资金、存货周转天数、应收账款周转天数。

ROE 杜邦拆分: ROE ≈ 净利润率 × 总资产周转率 × 权益乘数
  净利润率(netprofit_margin) / 总资产周转率(assets_turn) / 毛利率(grossprofit_margin)
  资产负债率(debt_to_assets) / 周转天数(invturn_days, arturn_days) 来自 fina_indicator;
  权益乘数 = 总资产 / 归母股东权益; 流动资产/货币资金 来自 balancesheet;
  最近价格 = 该年最后一个交易日收盘价。
"""

from __future__ import annotations

import asyncio

import pandas as pd

from . import data_service as ds
from . import pg_service


async def _annual_fina(pro, ts_code: str, year: int) -> dict | None:
    """获取某年最新一期 (优先年报) fina_indicator 关键指标。"""
    try:
        df = await pro.fina_indicator(ts_code=ts_code, period=f"{year}1231")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("end_date", ascending=False).reset_index(drop=True)
    row = df.iloc[0]
    return {
        "end_date": str(row.get("end_date") or ""),
        "roe": ds._to_float(row.get("roe")),
        "net_margin": ds._to_float(row.get("netprofit_margin")),
        "assets_turn": ds._to_float(row.get("assets_turn")),
        "gross_margin": ds._to_float(row.get("grossprofit_margin")),
        "debt_to_assets": ds._to_float(row.get("debt_to_assets")),
        # 该账号 fina_indicator 无 invturn_days/arturn_days, 用 ar_turn 计算应收周转天数
        "ar_turn": ds._to_float(row.get("ar_turn")),
        "fcff": ds._to_float(row.get("fcff")),
    }


async def _annual_balance(pro, ts_code: str, year: int) -> dict | None:
    """获取某年年报资产负债表关键科目 (总资产/权益为元, 流动资产/现金转万元)。"""
    try:
        df = await pro.balancesheet(ts_code=ts_code, period=f"{year}1231")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("end_date", ascending=False).reset_index(drop=True)
    row = df.iloc[0]
    return {
        "total_assets": ds._to_float(row.get("total_assets")),       # 元 (仅用于权益乘数比值)
        # balancesheet 字段为 total_hldr_eqy_exc_min_int (归属母公司股东权益, 元)
        "equity": ds._to_float(row.get("total_hldr_eqy_exc_min_int")),
        "inventories": ds._to_float(row.get("inventories")),         # 存货 (元)
        # 流动资产/现金单位: 元 -> 万元 (与 free_cashflow 一致)
        "total_cur_assets_wan": (ds._to_float(row.get("total_cur_assets")) or 0) / 10000.0 or None,
        "money_cap_wan": (ds._to_float(row.get("money_cap")) or 0) / 10000.0 or None,
    }


async def _annual_income(pro, ts_code: str, year: int) -> dict | None:
    """获取某年年报利润表营业成本 (元), 用于存货周转天数。"""
    try:
        df = await pro.income(ts_code=ts_code, period=f"{year}1231")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("end_date", ascending=False).reset_index(drop=True)
    # income 接口营业成本字段为 oper_cost
    return {"oper_cost": ds._to_float(df.iloc[0].get("oper_cost"))}


async def _year_end_close(pro, ts_code: str, year: int) -> float | None:
    """该年最后一个交易日收盘价。"""
    try:
        df = await pro.daily(ts_code=ts_code, start_date=f"{year}0101", end_date=f"{year}1231")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("trade_date").reset_index(drop=True)
    return ds._to_float(df.iloc[-1].get("close"))


async def compute_stock_row(pro, ts_code: str, symbol: str, name: str,
                            industry: str, year: int) -> dict | None:
    """计算单只股票单年的基本面指标, 返回入库行。"""
    fina = await _annual_fina(pro, ts_code, year)
    if fina is None:
        return None
    bs = await _annual_balance(pro, ts_code, year)
    inc = await _annual_income(pro, ts_code, year)

    # 权益乘数 = 总资产 / 归母股东权益
    equity_multiplier = None
    if bs and bs["total_assets"] and bs["equity"] and bs["equity"] > 0:
        equity_multiplier = bs["total_assets"] / bs["equity"]

    # 应收周转天数 = 365 / 应收周转率
    arturn_days = None
    if fina["ar_turn"] and fina["ar_turn"] > 0:
        arturn_days = 365.0 / fina["ar_turn"]

    # 存货周转天数 = 365 × 期末存货 / 营业成本
    invturn_days = None
    if bs and inc and bs["inventories"] and inc["oper_cost"] and inc["oper_cost"] > 0:
        invturn_days = 365.0 * bs["inventories"] / inc["oper_cost"]

    close = await _year_end_close(pro, ts_code, year)

    return {
        "ts_code": ts_code,
        "symbol": symbol,
        "name": name,
        "industry": industry or "",
        "year": year,
        "close": close,
        "roe": fina["roe"],
        "net_margin": fina["net_margin"],
        "assets_turn": fina["assets_turn"],
        "equity_multiplier": equity_multiplier,
        "gross_margin": fina["gross_margin"],
        # tushare fcff 单位为元, 转换为万元存储 (与 free_cashflow 单位一致)
        "free_cashflow": (fina["fcff"] or 0) / 10000.0 or None,
        "debt_to_assets": fina["debt_to_assets"],
        "total_cur_assets": bs["total_cur_assets_wan"] if bs else None,
        "money_cap": bs["money_cap_wan"] if bs else None,
        "invturn_days": invturn_days,
        "arturn_days": arturn_days,
        "end_date": fina["end_date"],
    }


# ---------------------------------------------------------------------------
# 同步 & 查询
# ---------------------------------------------------------------------------

async def _candidates(pro, industry: str, max_stocks: int) -> pd.DataFrame:
    stocks = await ds._stock_basic()
    if industry:
        cand = stocks[stocks["industry"].str.contains(industry, na=False)]
    else:
        cand = stocks
    return cand.head(max_stocks)


async def sync_industry_year(industry: str, year: int, max_stocks: int = 500,
                             sleep: float = 0.1) -> dict:
    """对指定行业+年份的全部公司计算并写入 PG (幂等 upsert)。"""
    pro = ds._init_pro()
    cand = await _candidates(pro, industry, max_stocks)
    rows = []
    failed = 0
    for _, row in cand.iterrows():
        r = await compute_stock_row(pro, row["ts_code"], row["symbol"], row["name"],
                                    row.get("industry", ""), year)
        if r is None:
            failed += 1
            continue
        rows.append(r)
        if sleep > 0:
            await asyncio.sleep(sleep)
    stored = await pg_service.upsert_fundamental_rows(rows)
    return {"industry": industry, "year": year, "scanned": int(len(cand)),
            "stored": stored, "failed": failed}


async def sync_industry_years(industry: str, years: list[int], max_stocks: int = 500,
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
        stocks = await ds._stock_basic()
        return int(stocks["industry"].str.contains(industry, na=False).sum())
    except Exception:
        return None


async def ensure_data(industry: str, years: list[int], max_stocks: int = 500) -> dict:
    """确保行业+各年份数据已入库; 缺失或记录数不足时自动同步补齐。"""
    expected = await _industry_stock_count(industry)
    missing: list[int] = []
    for y in years:
        count = await pg_service.count_fundamental_by_industry_year(industry, y)
        if count == 0 or (expected is not None and count < expected):
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
    return await pg_service.query_fundamental(industry, years, sort_by=sort_by,
                                              order=order, limit=limit, filters=filters)


# ---------------------------------------------------------------------------
# 正确性校验 (ROE 杜邦拆分一致性)
# ---------------------------------------------------------------------------

def verify_roe(items: list[dict], tol: float = 2.0) -> dict:
    """校验 ROE 拆分: roe ≈ net_margin × assets_turn × equity_multiplier。

    返回每项计算值与误差, 及误差>tol 的异常项。
    """
    checked, passed, bad = 0, 0, []
    for it in items:
        roe = it.get("roe")
        nm = it.get("net_margin")
        at = it.get("assets_turn")
        em = it.get("equity_multiplier")
        if None in (roe, nm, at, em) or at == 0:
            continue
        computed = nm * at * em  # 单位 %: 净利润率% * 周转率 * 权益乘数
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
