"""tushare 数据服务层。

职责:
  - 从 ../.env 读取 TUSHARE_TOKEN 并初始化 tushare pro 接口
  - 股票代码解析: 6 位代码 → ts_code + 名称 (支持股票 / ETF)
  - 日线数据获取 (股票 daily / ETF fund_daily), 带内存缓存
  - 股票关键字搜索 (前端联想)

约定 (与 strategy/ 下脚本一致):
  - token 从项目根目录 .env 读取 (TUSHARE_TOKEN)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import tushare as ts

# 项目根目录 (api/ 的上一级)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_token() -> None:
    """加载 ../.env 中的环境变量 (若未设置)。"""
    if os.getenv("TUSHARE_TOKEN"):
        return
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def _init_pro():
    _load_env_token()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置 TUSHARE_TOKEN, 请在项目根目录 .env 中配置。")
    ts.set_token(token)
    return ts.pro_api(token)


# ---------------------------------------------------------------------------
# 股票基本信息 / 代码解析
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _stock_basic() -> pd.DataFrame:
    """获取全部上市股票基本信息 (缓存)。"""
    pro = _init_pro()
    return pro.stock_basic(exchange="", list_status="L",
                           fields="ts_code,symbol,name,industry,market,list_date")


@lru_cache(maxsize=1)
def _fund_basic() -> pd.DataFrame:
    """获取全部上市基金基本信息 (缓存, 用于 ETF 解析)。"""
    pro = _init_pro()
    try:
        df = pro.fund_basic(market="E")
    except Exception:
        df = pd.DataFrame(columns=["ts_code", "name"])
    # fund_basic 无 symbol 列, 由 ts_code 推导 (如 513050.SH → 513050)
    if "symbol" not in df.columns:
        df["symbol"] = df["ts_code"].str.split(".").str[0]
    return df


def resolve_code(code: str) -> dict:
    """解析股票/ETF 代码, 返回 {"ts_code", "symbol", "name", "kind"}。

    支持两种输入:
      - 6 位数字代码, 如 "000858" → "000858.SZ" (五粮液)
      - 带后缀代码, 如 "000858.SZ" / "513050.SH"
    """
    code = code.strip().upper()
    if not code:
        raise ValueError("股票代码不能为空。")

    # 规范输入: 去掉空格等
    ts_code = code if "." in code else None

    stocks = _stock_basic()
    funds = _fund_basic()

    # 1) 6 位数字代码: 在股票 / 基金表中查找
    if ts_code is None:
        hit = stocks[stocks["symbol"] == code]
        if not hit.empty:
            row = hit.iloc[0]
            return {"ts_code": row["ts_code"], "symbol": code, "name": row["name"], "kind": "stock"}
        hit_f = funds[funds["symbol"] == code]
        if not hit_f.empty:
            row = hit_f.iloc[0]
            return {"ts_code": row["ts_code"], "symbol": code, "name": row["name"], "kind": "fund"}
        raise ValueError(f"未找到代码 [{code}] 对应的股票/基金。")

    # 2) 带后缀代码: 校验存在性
    hit = stocks[stocks["ts_code"] == ts_code]
    if not hit.empty:
        row = hit.iloc[0]
        return {"ts_code": ts_code, "symbol": row["symbol"], "name": row["name"], "kind": "stock"}
    hit_f = funds[funds["ts_code"] == ts_code]
    if not hit_f.empty:
        row = hit_f.iloc[0]
        return {"ts_code": ts_code, "symbol": row["symbol"], "name": row["name"], "kind": "fund"}
    raise ValueError(f"未找到代码 [{ts_code}] 对应的股票/基金。")


# A股股票代码前缀: 6(SH) 0/3(SZ) 4/8(北交所); 其余 1/5 开头多为基金/ETF
FUND_PREFIXES = ("51", "56", "58", "50", "15", "16", "18", "159", "160", "161", "162", "163", "164", "165", "166", "167", "168", "169", "180", "181", "182", "183", "184", "185", "186", "187", "188", "189")


def _is_fund_code(ts_code: str) -> bool:
    """判断 ts_code (如 513050.SH) 是否为基金/ETF 代码。"""
    symbol = ts_code.split(".")[0]
    return symbol.startswith(FUND_PREFIXES)


def search_stock(keyword: str, limit: int = 20) -> list[dict]:
    """按代码或名称模糊搜索股票/基金, 供前端联想使用。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    stocks = _stock_basic()
    mask = (
        stocks["symbol"].str.contains(keyword, na=False)
        | stocks["ts_code"].str.contains(keyword, na=False)
        | stocks["name"].str.contains(keyword, na=False)
    )
    hits = stocks[mask].head(limit)
    items = [
        {"ts_code": row["ts_code"], "symbol": row["symbol"], "name": row["name"], "kind": "stock"}
        for _, row in hits.iterrows()
    ]

    # 若股票结果不足, 补充基金/ETF (常见于输入 5/1 开头代码或基金名称)
    if len(items) < limit:
        funds = _fund_basic()
        fmask = (
            funds["symbol"].str.contains(keyword, na=False)
            | funds["ts_code"].str.contains(keyword, na=False)
            | funds["name"].str.contains(keyword, na=False)
        )
        fund_items = [
            {"ts_code": row["ts_code"], "symbol": row["symbol"], "name": row["name"], "kind": "fund"}
            for _, row in funds[fmask].head(limit - len(items)).iterrows()
        ]
        items.extend(fund_items)
    return items[:limit]


# ---------------------------------------------------------------------------
# 日线数据 (带缓存)
# ---------------------------------------------------------------------------

_DAILY_CACHE: dict[str, pd.DataFrame] = {}


def get_daily(ts_code: str, kind: str = "stock",
              start_date: str = "20170101", end_date: str = "") -> pd.DataFrame:
    """获取单只股票/ETF 日线 (trade_date 升序), 带内存缓存。"""
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    cache_key = f"{ts_code}:{kind}:{start_date}:{end_date}"
    if cache_key in _DAILY_CACHE:
        return _DAILY_CACHE[cache_key]

    pro = _init_pro()
    df = pd.DataFrame()
    if kind == "fund" or _is_fund_code(ts_code):
        try:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception:
            df = pd.DataFrame()
        if df.empty:
            df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    else:
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df.empty:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

    if df is None or df.empty:
        raise ValueError(f"未获取到 {ts_code} 在 {start_date}~{end_date} 的日线数据。")

    df = df.sort_values("trade_date").reset_index(drop=True)
    _DAILY_CACHE[cache_key] = df
    return df


def get_quote(ts_code: str, kind: str = "stock", days: int = 120) -> pd.DataFrame:
    """获取最近 N 个交易日的行情数据 (升序), 供前端绘制行情曲线。"""
    end_date = datetime.now().strftime("%Y%m%d")
    # 按约 2.5 倍自然日预取, 以覆盖 N 个交易日
    start = (datetime.now() - timedelta(days=int(days * 2.5) + 30)).strftime("%Y%m%d")
    df = get_daily(ts_code, kind, start_date=start, end_date=end_date)
    return df.tail(days).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 基本面选股 (资产负债率 / ROE / 分红率)
# ---------------------------------------------------------------------------

MAX_SCAN_STOCKS = 500      # 未指定行业时最多扫描的股票数 (避免全市场逐只查询过慢)
_SCAN_SLEEP = 0.12         # 逐只查询 tushare 的时间间隔 (秒), 防频率限制


def _to_float(v) -> float | None:
    """安全转 float, 无效值返回 None。"""
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _fina_latest(pro, ts_code: str, period: str = "") -> dict | None:
    """获取单只股票最新一期 (或指定报告期) 财务指标。

    未指定 period 时优先取最新年报 (end_date 以 1231 结尾), 保证 ROE 为全年口径;
    若无年报数据则退回最新一期。返回含 roe/debt_to_assets/dt_eps/end_date 的 dict。
    """
    try:
        if period:
            df = pro.fina_indicator(ts_code=ts_code, period=period)
        else:
            df = pro.fina_indicator(ts_code=ts_code)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("end_date", ascending=False).reset_index(drop=True)
    if not period:
        annual = df[df["end_date"].astype(str).str.endswith("1231")]
        if not annual.empty:
            df = annual
    row = df.iloc[0]
    return {
        "end_date": str(row.get("end_date") or ""),
        "debt_to_assets": _to_float(row.get("debt_to_assets")),
        "roe": _to_float(row.get("roe")),
        "dt_eps": _to_float(row.get("dt_eps")),
        "fcff": _to_float(row.get("fcff")),
    }


def _dividend_latest(pro, ts_code: str) -> dict | None:
    """获取单只股票最近一次实施的分红记录。

    dividend 接口返回同一分红年度的多条流程记录 (预案/股东大会/实施),
    优先取 div_proc='实施' 的记录; 若无实施记录则取该年度 cash_div 最大的记录。
    """
    try:
        dv = pro.dividend(ts_code=ts_code)
    except Exception:
        return None
    if dv is None or dv.empty:
        return None
    dv = dv.sort_values("end_date", ascending=False).reset_index(drop=True)
    end = str(dv.iloc[0]["end_date"])
    yearly = dv[dv["end_date"].astype(str) == end].copy()
    yearly["_cash"] = pd.to_numeric(yearly.get("cash_div"), errors="coerce")
    impl = yearly[yearly["div_proc"] == "实施"]
    if not impl.empty:
        row = impl.sort_values("_cash", ascending=False).iloc[0]
    else:
        row = yearly.sort_values("_cash", ascending=False).iloc[0]
    return {"end_date": end, "cash_div": _to_float(row.get("cash_div"))}


def screen_by_fundamentals(industry: str = "", period: str = "",
                           max_debt_to_assets: float = 60.0,
                           min_roe: float = 10.0,
                           min_payout_ratio: float = 30.0,
                           max_stocks: int = 100,
                           limit: int = 100) -> dict:
    """按 资产负债率 ≤ / ROE ≥ / 分红率 ≥ 筛选股票。

    - industry: 东财行业名称, 空串表示全市场 (最多扫描 max_stocks 只)
    - period: 报告期 YYYYMMDD, 空串表示每只取最新年报 (无年报则最新一期)
    - 分红率 (%) = 每股现金红利 / 每股收益 (dt_eps) × 100
    """
    pro = _init_pro()
    stocks = _stock_basic()

    if industry:
        cand = stocks[stocks["industry"].str.contains(industry, na=False)].copy()
    else:
        cand = stocks.copy()
    cand = cand.head(min(int(max_stocks) or MAX_SCAN_STOCKS, len(cand)))

    results = []
    scanned = 0
    for _, row in cand.iterrows():
        ts_code = row["ts_code"]
        fina = _fina_latest(pro, ts_code, period)
        scanned += 1
        if fina is None:
            continue

        debt = fina["debt_to_assets"]
        roe = fina["roe"]
        eps = fina["dt_eps"]

        # 分红率 = 每股现金红利 / 每股收益 × 100
        payout = None
        div = _dividend_latest(pro, ts_code)
        if div is not None and div["cash_div"] is not None:
            if eps and eps > 0:
                payout = div["cash_div"] / eps * 100.0

        # --- 过滤 ---
        if max_debt_to_assets is not None and debt is not None and debt > max_debt_to_assets:
            continue
        if min_roe is not None and roe is not None and roe < min_roe:
            continue
        # 要求分红率时, 数据缺失视为不满足
        if min_payout_ratio:
            if payout is None or payout < min_payout_ratio:
                continue

        results.append({
            "ts_code": ts_code,
            "symbol": row["symbol"],
            "name": row["name"],
            "industry": row.get("industry", ""),
            "end_date": fina["end_date"],
            "debt_to_assets": debt,
            "roe": roe,
            "payout_ratio": payout,
        })

        time.sleep(_SCAN_SLEEP)

    # 排序: ROE 高者优先, 缺失排后
    results.sort(key=lambda x: (x["roe"] is not None, x["roe"] if x["roe"] is not None else -1),
                 reverse=True)

    return {
        "scanned": scanned,
        "matched": len(results),
        "industry": industry,
        "items": results[:limit],
    }
