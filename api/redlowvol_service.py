"""红利低波选股服务: 指标计算 + 数据同步到 PostgreSQL。

参考中证红利低波动指数 (H30269) 思路, 对指定行业/年份的每家公司计算:
  股息率、波动率、每股分红、自由现金流、EPS、分红率、ROE、资产负债率、
  日均市值、日均成交额 等关键指标, 写入 pg_service 管理的 red_low_vol 表。

数据来源 (tushare):
  - pro.stock_basic       行业成分股
  - pro.daily             日线 (计算年化波动率 / 年末收盘价)
  - pro.daily_basic       每日指标 (总市值 / 成交额)
  - pro.fina_indicator    财务指标 (roe / debt_to_assets / dt_eps / fcff)
  - pro.dividend          分红 (每股现金红利 cash_div)
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta

import pandas as pd

from . import data_service as ds
from . import pg_service

# 波动率计算使用的年化因子 (252 交易日)
ANNUALIZATION = math.sqrt(252)


# ---------------------------------------------------------------------------
# 指标计算 (单只股票 / 单年)
# ---------------------------------------------------------------------------

def _year_daily(pro, ts_code: str, year: int) -> pd.DataFrame | None:
    """获取某年日线 (升序), 供波动率 / 年末收盘价计算。"""
    try:
        df = pro.daily(ts_code=ts_code, start_date=f"{year}0101", end_date=f"{year}1231")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df.sort_values("trade_date").reset_index(drop=True)


def _year_daily_basic(pro, ts_code: str, year: int) -> pd.DataFrame | None:
    """获取某年每日指标 (总市值/成交额/股息率等), 显式指定所需字段。"""
    try:
        df = pro.daily_basic(
            ts_code=ts_code, start_date=f"{year}0101", end_date=f"{year}1231",
            fields="ts_code,trade_date,close,total_mv,circ_mv,amount,dv_ratio,dv_ttm",
        )
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df.reset_index(drop=True)


def _compute_volatility(daily: pd.DataFrame | None) -> tuple[float | None, float | None]:
    """返回 (年末收盘价, 年化波动率 %)。"""
    if daily is None or daily.empty:
        return None, None
    closes = pd.to_numeric(daily["close"], errors="coerce").dropna()
    if closes.empty:
        return None, None
    year_end_close = float(closes.iloc[-1])
    rets = closes.pct_change().dropna()
    if len(rets) < 2:
        return year_end_close, None
    vol = float(rets.std(ddof=1)) * ANNUALIZATION * 100.0
    return year_end_close, vol


def _avg_mv_amt(basic: pd.DataFrame | None, daily: pd.DataFrame | None) -> tuple[float | None, float | None]:
    """返回 (日均总市值万元, 日均成交金额万元)。

    total_mv 来自 daily_basic (万元); amount 成交额来自 daily (千元 → 万元 /10),
    因为部分账号的 daily_basic 不返回 amount 字段。
    """
    avg_mv = None
    if basic is not None and not basic.empty and "total_mv" in basic.columns:
        try:
            s = pd.to_numeric(basic["total_mv"], errors="coerce").dropna()
            avg_mv = float(s.mean()) if not s.empty else None
        except Exception:
            avg_mv = None

    avg_amt = None
    if daily is not None and not daily.empty and "amount" in daily.columns:
        try:
            s = pd.to_numeric(daily["amount"], errors="coerce").dropna()
            if not s.empty:
                avg_amt = float(s.mean() / 10.0)
        except Exception:
            avg_amt = None
    return avg_mv, avg_amt


def _latest_close(pro, ts_code: str) -> float | None:
    """最近一个交易日 (上个交易日) 的收盘价, 用作股息率-TTM 分母。"""
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
        df = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_date")
        return float(df.iloc[-1]["close"])
    except Exception:
        return None


def _div_per_share(pro, ts_code: str, year: int) -> float | None:
    """该分红年度 (end_date 年份==year) 每股现金红利之和 (元)。

    同一分红年度存在 中期(0630)/三季(0930)/年度(1231) 多期分红, 每期又有
    预案/股东大会/实施 多条流程记录。按 end_date 年份聚合所有'实施'记录求和
    (一年多次分红), 无实施记录时退回该年 cash_div 最大值。
    """
    return ds._annual_div_per_share(pro, ts_code, year)


def _dividend_growth_3y(pro, ts_code: str, year: int) -> float | None:
    """3 年每股股利复合增长率 % = (当年分红 / 三年前分红)^(1/3) - 1。"""
    try:
        dv = pro.dividend(ts_code=ts_code)
    except Exception:
        return None
    if dv is None or dv.empty:
        return None
    div_by_year: dict[int, float] = {}
    dv = dv.copy()
    if "cash_div" not in dv.columns:
        return None
    dv["_cash"] = pd.to_numeric(dv.get("cash_div"), errors="coerce")
    impl = dv[dv["div_proc"] == "实施"]
    # 同 end_date 重复记录 (tushare 偶发) 去重后再求和, 避免同一笔分红重复计入
    impl = impl.drop_duplicates(subset=["end_date", "cash_div"])
    for _, r in impl.iterrows():
        try:
            y = int(str(r["end_date"])[:4])
        except (TypeError, ValueError):
            continue
        c = ds._to_float(r.get("cash_div"))
        if c is not None:
            # 一年多次分红需求和, 不能用 max (原 max 只取单期)
            div_by_year[y] = div_by_year.get(y, 0.0) + c
    d0 = div_by_year.get(year)
    d3 = div_by_year.get(year - 3)
    if not d0 or not d3 or d0 <= 0 or d3 <= 0:
        return None
    try:
        return (pow(d0 / d3, 1.0 / 3.0) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        return None


def compute_stock_row(pro, ts_code: str, symbol: str, name: str,
                      industry: str, year: int, last_close: float | None = None) -> dict | None:
    """计算单只股票单年的红利低波指标, 返回入库行 (缺数据字段为 None)。

    last_close: 上个交易日收盘价 (股息率-TTM 分母); 不传则自动拉取。
    """
    daily = _year_daily(pro, ts_code, year)
    year_end_close, volatility = _compute_volatility(daily)

    basic = _year_daily_basic(pro, ts_code, year)
    avg_mv, avg_amt = _avg_mv_amt(basic, daily)

    fina = ds._fina_latest(pro, ts_code, period=f"{year}1231")
    eps = fina["dt_eps"] if fina else None
    roe = fina["roe"] if fina else None
    debt = fina["debt_to_assets"] if fina else None
    fcff = fina["fcff"] if fina else None
    # tushare fcff 单位为元, 统一转换为万元存储 (与 total_mv / amount 单位一致)
    free_cashflow = fcff / 10000.0 if fcff is not None else None
    end_date = fina["end_date"] if fina else ""

    div_per_share = _div_per_share(pro, ts_code, year)
    div_growth = _dividend_growth_3y(pro, ts_code, year)

    # 静态股息率 = 全年每股分红 / 年末收盘价 × 100
    dividend_yield = None
    if div_per_share is not None and year_end_close:
        dividend_yield = div_per_share / year_end_close * 100.0

    # 股息率-TTM = 全年每股分红 / 上个交易日收盘价 × 100
    if last_close is None:
        last_close = _latest_close(pro, ts_code)
    dividend_yield_ttm = None
    if div_per_share is not None and last_close:
        dividend_yield_ttm = div_per_share / last_close * 100.0

    payout_ratio = None
    if div_per_share is not None and eps and eps > 0:
        payout_ratio = div_per_share / eps * 100.0

    return {
        "ts_code": ts_code,
        "symbol": symbol,
        "name": name,
        "industry": industry or "",
        "year": year,
        "dividend_yield": dividend_yield,
        "dividend_yield_ttm": dividend_yield_ttm,
        "last_close": last_close,
        "volatility": volatility,
        "div_per_share": div_per_share,
        "free_cashflow": free_cashflow,
        "eps": eps,
        "payout_ratio": payout_ratio,
        "dividend_growth_3y": div_growth,
        "roe": roe,
        "debt_to_assets": debt,
        "avg_daily_mv": avg_mv,
        "avg_daily_amt": avg_amt,
        "end_date": end_date,
    }


# ---------------------------------------------------------------------------
# 同步 & 查询
# ---------------------------------------------------------------------------

def sync_industry_year(industry: str, year: int, max_stocks: int = 500,
                       sleep: float = 0.1) -> dict:
    """对指定行业+年份的全部公司计算指标并写入 PG (幂等 upsert)。

    行业为空时同步全市场前 max_stocks 只 (较慢)。
    """
    pro = ds._init_pro()
    stocks = ds._stock_basic()
    if industry:
        cand = stocks[stocks["industry"].str.contains(industry, na=False)]
    else:
        cand = stocks
    cand = cand.head(max_stocks)

    rows = []
    failed = 0
    for _, row in cand.iterrows():
        last_close = _latest_close(pro, row["ts_code"])
        r = compute_stock_row(pro, row["ts_code"], row["symbol"], row["name"],
                              row.get("industry", ""), year, last_close=last_close)
        if r is None:
            failed += 1
            continue
        rows.append(r)
        if sleep > 0:
            time.sleep(sleep)

    stored = pg_service.upsert_rows(rows)
    return {
        "industry": industry,
        "year": year,
        "scanned": int(len(cand)),
        "stored": stored,
        "failed": failed,
    }


def sync_industry_years(industry: str, years: list[int], max_stocks: int = 500,
                        sleep: float = 0.1) -> dict:
    """对指定行业+多个年份逐期同步 (幂等 upsert), 返回各年份统计。"""
    total = {"industry": industry, "years": years,
             "stored_total": 0, "scanned_total": 0, "per_year": {}}
    for y in years:
        r = sync_industry_year(industry, y, max_stocks=max_stocks, sleep=sleep)
        total["stored_total"] += r["stored"]
        total["scanned_total"] += r["scanned"]
        total["per_year"][str(y)] = r
    return total


def ensure_data(industry: str, years: list[int], max_stocks: int = 500) -> dict:
    """确保行业+各年份数据已入库; 缺失或记录数少于行业股票数时自动同步补齐。"""
    expected = _industry_stock_count(industry)
    missing: list[int] = []
    for y in years:
        count = pg_service.count_by_industry_year(industry, y)
        if count == 0 or (expected is not None and count < expected):
            missing.append(y)

    stored_total = 0
    per_year = {}
    for y in missing:
        r = sync_industry_year(industry, y, max_stocks=max_stocks)
        stored_total += r["stored"]
        per_year[str(y)] = r

    return {"synced": bool(missing), "years": years, "missing": missing,
            "stored": stored_total, "per_year": per_year}


def _industry_stock_count(industry: str) -> int | None:
    """行业股票总数 (全市场为 None, 表示无法预判)。"""
    if not industry:
        return None
    try:
        stocks = ds._stock_basic()
        return int(stocks["industry"].str.contains(industry, na=False).sum())
    except Exception:
        return None


def screen(industry: str, years: list[int], sort_by: str = "dividend_yield",
           order: str = "desc", filters: dict | None = None,
           limit: int = 500) -> list[dict]:
    """从 PG 查询该行业+多个年份全部公司, 支持字段阈值筛选, 按指定指标排序。"""
    return pg_service.query_screen(industry, years, sort_by=sort_by,
                                   order=order, limit=limit, filters=filters)
