"""港股数据服务层 (港股红利低波 / 基本面选股数据源)。

港股数据源说明 (本机网络验证结果):
  - tushare `hk_basic`: 港股股票列表 (2782 只)。注意 hk_daily 限频 1 次/小时,
    故日线不用 tushare, 改用腾讯港股 K 线。
  - 腾讯港股 K 线 (web.ifzq.gtimg.cn): 日线 (波动率/年末收盘/最新收盘), 不复权。
  - 东方财富 datacenter (datacenter.eastmoney.com): 港股财务指标 / 分红 / 资产负债表 / 行业。
    push2/push2his 行情域在本机不可达, 但 datacenter 域可用。
      * RPT_HKF10_FN_MAININDICATOR  主要财务指标 (ROE/毛利率/净利率/权益乘数/负债率/EPS/市值等)
      * RPT_HKF10_MAIN_DIVBASIC     分红派息历史 (每股现金分红, 按财政年度聚合)
      * RPT_HKF10_FN_BALANCE_PC     资产负债表 (流动资产/现金/存货等)
      * RPT_HKF10_INFO_ORGPROFILE   公司资料 (所属行业, 可批量获取)

约定:
  - ts_code 统一为带后缀格式, 如 00700.HK (5 位) / 00005.HK (5 位带前导0)
  - 金额单位统一为 万港元 (与 A 股 万元 口径一致); 价格/每股分红/每股收益单位为 港元
  - 比率单位 % (ROE/毛利率/净利率/负债率/股息率/波动率/分红率等)
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

from . import data_service

# 项目缓存目录 (股票列表/行业映射等, 避免频繁调用限频接口)
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# 年化因子 (港股交易日约 252)
ANNUALIZATION = math.sqrt(252)

# 东方财富 datacenter API
EM_DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"

# 腾讯港股 K 线
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com",
}

# 复用连接 (HTTP keep-alive), 显著降低同一 host 的请求延迟
_SESSION = requests.Session()
_SESSION.headers.update(_UA)

# USD→HKD 兜底汇率 (仅当分红方案只给美元、无港元折算时使用)
USD_HKD_FALLBACK = 7.8


# ---------------------------------------------------------------------------
# 基础: tushare
# ---------------------------------------------------------------------------

def _init_pro():
    """复用 A 股数据层的 tushare pro 接口 (同一 token)。"""
    return data_service._init_pro()


def hk_stock_list(use_cache: bool = True) -> pd.DataFrame:
    """港股股票列表 (tushare hk_basic), 默认带本地缓存 (避免频繁调用限频接口)。

    返回列: ts_code, symbol, name, market, list_status, list_date, trade_unit, curr_type。
    """
    cache_path = CACHE_DIR / "hk_stock_list.json"
    if use_cache and cache_path.exists():
        # convert_dates=False: list_date 等日期字符串保持原样, 避免 pandas 逐个推断格式告警
        df = pd.read_json(cache_path, dtype={"ts_code": str, "symbol": str}, convert_dates=False)
        if not df.empty:
            return df
    pro = _init_pro()
    df = pro.hk_basic(fields="ts_code,name,market,list_status,list_date,trade_unit,curr_type")
    if df is None or df.empty:
        raise RuntimeError("tushare hk_basic 返回空 (可能限频或无权限)。")
    df = df.reset_index(drop=True)
    # symbol 由 ts_code 推导 (00700.HK → 00700)
    if "symbol" not in df.columns:
        df["symbol"] = df["ts_code"].str.split(".").str[0]
    df.to_json(cache_path, orient="records", force_ascii=False)
    return df


def _symbol_to_ts_code(symbol: str) -> str:
    """5 位港股代码 → ts_code (00005 → 00005.HK); 兼容 4 位 (无前导0, 如 700 → 00700)。"""
    s = str(symbol).strip().upper().replace(".HK", "")
    if s.isdigit():
        s = s.zfill(5)
    return f"{s}.HK"


# ---------------------------------------------------------------------------
# 基础: 东方财富 datacenter
# ---------------------------------------------------------------------------

def _em_request(report_name: str, columns: str = "ALL", filter_: str = "",
                page: int = 1, page_size: int = 50, sort_columns: str = "",
                sort_types: str = "", retries: int = 3) -> dict:
    """调用东财 datacenter 数据接口, 带重试。"""
    params = {
        "reportName": report_name,
        "columns": columns,
        "quoteColumns": "",
        "filter": filter_,
        "pageNumber": str(page),
        "pageSize": str(page_size),
        "sortTypes": sort_types,
        "sortColumns": sort_columns,
        "source": "F10",
        "client": "PC",
        "v": str(int(time.time() * 1000)),
    }
    for attempt in range(retries):
        try:
            r = _SESSION.get(EM_DATACENTER_URL, params=params, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json()
        except Exception:
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return {}


def _em_data(report_name: str, columns: str = "ALL", filter_: str = "",
             page: int = 1, page_size: int = 50, sort_columns: str = "",
             sort_types: str = "") -> list[dict]:
    """东财 datacenter 数据列表 (自动从 result.data 提取, 空返回 [])。"""
    j = _em_request(report_name, columns, filter_, page, page_size, sort_columns, sort_types)
    res = j.get("result") or {}
    return res.get("data") or []


def _to_float(v) -> float | None:
    """安全转 float, 空/异常返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f  # NaN → None
    except (TypeError, ValueError):
        return None


def _wan(v) -> float | None:
    """元 → 万港元 (与 A 股万元口径一致); 空返回 None。"""
    f = _to_float(v)
    return f / 10000.0 if f is not None else None


# ---------------------------------------------------------------------------
# 财务指标 (RPT_HKF10_FN_MAININDICATOR) — 一次调用返回全部年度
# ---------------------------------------------------------------------------

_MAIN_INDICATOR_COLS = (
    "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,STD_REPORT_DATE,REPORT_DATE,DATE_TYPE_CODE,"
    "BASIC_EPS,DILUTED_EPS,EPS_TTM,BPS,OPERATE_INCOME,OPERATE_INCOME_YOY,GROSS_PROFIT,"
    "GROSS_PROFIT_RATIO,HOLDER_PROFIT,HOLDER_PROFIT_YOY,NET_PROFIT_RATIO,ROE_AVG,ROE_YEARLY,ROA,"
    "TOTAL_ASSETS,TOTAL_LIABILITIES,TOTAL_PARENT_EQUITY,DEBT_ASSET_RATIO,EQUITY_MULTIPLIER,"
    "CURRENT_RATIO,CURRENT_ASSETS_TDAYS,INVENTORY_TDAYS,ACCOUNTS_RECE_TDAYS,"
    "NETCASH_OPERATE,NETCASH_INVEST,NETCASH_FINANCE,PER_NETCASH_OPERATE,"
    "PE_TTM,PB_TTM,TOTAL_MARKET_CAP,ISSUED_COMMON_SHARES,DIVI_RATIO,DPS_HKD,DIVIDEND_RATE"
)


def _fina_indicator_map(ts_code: str) -> dict[int, dict]:
    """某港股各财政年度主要财务指标 {year: {字段: 值}} (仅年报 DATE_TYPE_CODE=001)。

    金额字段 (OPERATE_INCOME/TOTAL_ASSETS/现金流/市值) 统一转 万港元。
    """
    code = ts_code.split(".")[0]
    rows = _em_data(
        "RPT_HKF10_FN_MAININDICATOR", columns=_MAIN_INDICATOR_COLS,
        filter_=f'(SECUCODE="{ts_code}")(DATE_TYPE_CODE="001")',
        page_size=100, sort_columns="STD_REPORT_DATE", sort_types="-1",
    )
    out: dict[int, dict] = {}
    for r in rows:
        end = str(r.get("STD_REPORT_DATE") or r.get("REPORT_DATE") or "")[:10]
        try:
            year = int(end[:4])
        except (TypeError, ValueError):
            continue
        if year < 1995:  # 过滤异常早期数据
            continue
        out[year] = {
            "end_date": end,
            "eps": _to_float(r.get("BASIC_EPS")),
            "bps": _to_float(r.get("BPS")),
            "roe": _to_float(r.get("ROE_AVG")),
            "roa": _to_float(r.get("ROA")),
            "or_yoy": _to_float(r.get("OPERATE_INCOME_YOY")),
            "netprofit_yoy": _to_float(r.get("HOLDER_PROFIT_YOY")),
            # 杜邦分量统一用归母口径从原始分量计算, 保证 roe≈净利率×周转×权益乘数 内部一致:
            #   净利率 = 归母净利润/营业收入; 周转 = 营业收入/总资产; 权益乘数 = 总资产/归母权益
            "net_margin": None,
            "gross_margin": _to_float(r.get("GROSS_PROFIT_RATIO")),
            "debt_to_assets": _to_float(r.get("DEBT_ASSET_RATIO")),
            "equity_multiplier": None,
            "current_ratio": _to_float(r.get("CURRENT_RATIO")),
            "assets_turn": None,  # 由 OPERATE_INCOME/TOTAL_ASSETS 计算
            "invturn_days": _to_float(r.get("INVENTORY_TDAYS")),
            "arturn_days": _to_float(r.get("ACCOUNTS_RECE_TDAYS")),
            "operate_income_wan": _wan(r.get("OPERATE_INCOME")),
            "total_assets_wan": _wan(r.get("TOTAL_ASSETS")),
            "total_liab_wan": _wan(r.get("TOTAL_LIABILITIES")),
            "total_equity_wan": _wan(r.get("TOTAL_PARENT_EQUITY")),
            "net_profit_wan": _wan(r.get("HOLDER_PROFIT")),
            "ocf_wan": _wan(r.get("NETCASH_OPERATE")),
            "icf_wan": _wan(r.get("NETCASH_INVEST")),
            "fncf_wan": _wan(r.get("NETCASH_FINANCE")),
            "total_mv_wan": _wan(r.get("TOTAL_MARKET_CAP")),
            "issued_shares": _to_float(r.get("ISSUED_COMMON_SHARES")),
            "pe_ttm": _to_float(r.get("PE_TTM")),
            "pb": _to_float(r.get("PB_TTM")),
            "dps_hkd": _to_float(r.get("DPS_HKD")),
            "payout_ratio_em": _to_float(r.get("DIVI_RATIO")),
        }
        # 杜邦分量 (归母口径): 净利率 = 归母净利/营收; 周转 = 营收/总资产; 权益乘数 = 总资产/归母权益
        oi = out[year]["operate_income_wan"]
        ta = out[year]["total_assets_wan"]
        np_ = out[year]["net_profit_wan"]
        eq = out[year]["total_equity_wan"]
        if oi and ta:
            out[year]["assets_turn"] = oi / ta
            if np_ is not None:
                out[year]["net_margin"] = np_ / oi * 100.0
            if eq:
                out[year]["equity_multiplier"] = ta / eq
    return out


# ---------------------------------------------------------------------------
# 分红 (RPT_HKF10_MAIN_DIVBASIC) — 按财政年度聚合每股现金分红
# ---------------------------------------------------------------------------

# 分红方案解析: 优先 港币X元 (东财通常已给港元折算), 依次退 港元/HK$/每股派X元/仙/分
_DPS_PATTERNS = [
    re.compile(r"港币\s*([0-9.]+)\s*元"),
    re.compile(r"港元\s*([0-9.]+)\s*元"),
    re.compile(r"HK\$\s*([0-9.]+)"),
    re.compile(r"每股(?:派|派息|股息|派发)\s*([0-9.]+)\s*元"),
    re.compile(r"每股(?:派|派息|股息|派发)\s*([0-9.]+)\s*仙"),
    re.compile(r"每股(?:派|派息|股息|派发)\s*([0-9.]+)\s*分"),
]
_USD_PATTERN = re.compile(r"美元\s*([0-9.]+)\s*元")


def parse_dps(plan_explain: str) -> float | None:
    """从分红方案文本解析每股现金分红 (港元); 无现金分红 (送股/实物派发) 返回 None。

    例:
      "每股派港币5.3元"                      → 5.3
      "每股派美元0.1元(相当于港币0.784234元(计算值))" → 0.784234 (优先港元折算)
      "每股派美元0.1元"                      → 0.78 (USD×7.8 兜底)
      "特殊说明:每10股分派1股美团..."         → None (非现金)
    """
    text = (plan_explain or "").strip()
    if not text or "送股" in text or "分派" in text and "元" not in text:
        # 实物派发/送股 (如派美团/京东股份) 无现金 → 跳过
        if "每股派" not in text and "派息" not in text:
            return None
    for pat in _DPS_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                v = float(m.group(1))
            except (TypeError, ValueError):
                continue
            # 仙/分 → 元
            if "仙" in pat.pattern:
                v /= 100.0
            elif "分" in pat.pattern:
                v /= 100.0
            return v
    # 兜底: 仅美元
    m = _USD_PATTERN.search(text)
    if m:
        try:
            return float(m.group(1)) * USD_HKD_FALLBACK
        except (TypeError, ValueError):
            return None
    return None


def _dividend_map(ts_code: str) -> dict[int, float]:
    """某港股按财政年度聚合的每股现金分红 {year: dps_hkd}。

    RPT_HKF10_MAIN_DIVBASIC 的 YEAR 字段 = 分红所属财政年度 (港股跨年多次派息,
    如汇丰季度派息, 同一财政年度多次派息求和); 排除非现金派发 (送股/实物)。
    自动分页 (部分公司分红记录较多)。
    """
    code = ts_code.split(".")[0]
    all_rows: list[dict] = []
    page = 1
    while True:
        rows = _em_data(
            "RPT_HKF10_MAIN_DIVBASIC",
            columns=("SECURITY_CODE,UPDATE_DATE,REPORT_TYPE,EX_DIVIDEND_DATE,"
                     "DIVIDEND_DATE,TRANSFER_END_DATE,YEAR,PLAN_EXPLAIN,IS_BFP"),
            filter_=f'(SECURITY_CODE="{code}")(IS_BFP="0")',
            page=page, page_size=200, sort_columns="UPDATE_DATE", sort_types="-1",
        )
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 200:
            break
        page += 1
        time.sleep(0.2)
    out: dict[int, float] = {}
    for r in all_rows:
        try:
            year = int(str(r.get("YEAR") or "")[:4])
        except (TypeError, ValueError):
            continue
        dps = parse_dps(str(r.get("PLAN_EXPLAIN") or ""))
        if dps is None or dps <= 0:
            continue
        out[year] = out.get(year, 0.0) + dps
    return out


# ---------------------------------------------------------------------------
# 资产负债表 (RPT_HKF10_FN_BALANCE_PC) — 流动资产/现金/存货
# ---------------------------------------------------------------------------

# 关键科目代码: 流动资产合计 / 现金及等价物 / 存货 / 应收账款 / 流动负债 / 固定资产
_BAL_ITEMS = {
    "004002999": "total_cur_assets",   # 流动资产合计
    "004002010": "money_cap",          # 现金及等价物
    "004002001": "inventory",          # 存货
    "004002003": "accounts_receiv",    # 应收账款
    "004011999": "total_cur_liab",     # 流动负债合计
    "004001002": "fixed_assets",       # 物业厂房及设备
    "004009999": "total_assets",       # 总资产 (校验)
}


def _balance_map(ts_code: str) -> dict[int, dict]:
    """某港股各财政年度资产负债表关键科目 {year: {total_cur_assets_wan, money_cap_wan, inventory_wan}}。

    仅取年报 (DATE_TYPE_CODE=001), 自动分页 (page_size=2000, 通常一次取回全部年度)。
    """
    all_rows: list[dict] = []
    page = 1
    while True:
        rows = _em_data(
            "RPT_HKF10_FN_BALANCE_PC",
            columns=("SECUCODE,SECURITY_CODE,REPORT_DATE,STD_ITEM_CODE,STD_ITEM_NAME,"
                     "AMOUNT,DATE_TYPE_CODE"),
            filter_=f'(SECUCODE="{ts_code}")(DATE_TYPE_CODE="001")',
            page=page, page_size=2000, sort_columns="REPORT_DATE,STD_ITEM_CODE",
        )
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 2000:
            break
        page += 1
        time.sleep(0.1)
    out: dict[int, dict] = {}
    for r in all_rows:
        end = str(r.get("REPORT_DATE") or "")[:10]
        try:
            year = int(end[:4])
        except (TypeError, ValueError):
            continue
        # 仅保留年报 (12-31)
        if not end.endswith("12-31"):
            continue
        item_code = str(r.get("STD_ITEM_CODE") or "")
        field = _BAL_ITEMS.get(item_code)
        if not field:
            continue
        out.setdefault(year, {})
        out[year][field + "_wan"] = _wan(r.get("AMOUNT"))
    return out


# ---------------------------------------------------------------------------
# 行业 (RPT_HKF10_INFO_ORGPROFILE) — 批量获取, 带缓存
# ---------------------------------------------------------------------------

def industry_map(use_cache: bool = True) -> dict[str, str]:
    """全部港股 代码→行业 映射 {ts_code: industry}, 带本地缓存。

    一次批量请求 (约 140 页) 覆盖全部港股 (含主板/创业板), 结果按 tushare hk_basic 过滤。
    """
    cache_path = CACHE_DIR / "hk_industry.json"
    if use_cache and cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    stocks = hk_stock_list()
    valid = set(stocks["ts_code"].astype(str))
    mapping: dict[str, str] = {}
    page = 1
    while True:
        rows = _em_data(
            "RPT_HKF10_INFO_ORGPROFILE",
            columns="SECUCODE,SECURITY_CODE,ORG_NAME,BELONG_INDUSTRY",
            page=page, page_size=100,
        )
        if not rows:
            break
        for r in rows:
            ts_code = _symbol_to_ts_code(r.get("SECURITY_CODE"))
            if ts_code in valid:
                ind = str(r.get("BELONG_INDUSTRY") or "").strip()
                if ind:
                    mapping[ts_code] = ind
        if len(rows) < 100:
            break
        page += 1
        time.sleep(0.3)
    cache_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


# ---------------------------------------------------------------------------
# 日线 (腾讯港股 K 线) — 波动率/年末收盘/最新收盘
# ---------------------------------------------------------------------------

@lru_cache(maxsize=256)
def _tencent_kline_df(ts_code: str) -> pd.DataFrame:
    """腾讯港股日线 (不复权), 列: date/open/close/high/low/vol。

    腾讯 fqkline count 参数上限约 2000~3000, 超限返回空; 用 count=2000 取最近约 8 年
    (被截断时返回最近 2000 条), 足够近年选股 (2020+)。
    """
    symbol = ts_code.split(".")[0]
    end = datetime.now().strftime("%Y-%m-%d")
    params = {"param": f"hk{symbol},day,2000-01-01,{end},2000,"}
    try:
        r = requests.get(TENCENT_KLINE_URL, params=params, headers=_UA, timeout=20)
        j = r.json()
    except Exception:
        return pd.DataFrame()
    d = (j.get("data") or {}).get(f"hk{symbol}") or {}
    arr = d.get("day") or d.get("qfqday") or []
    if not arr:
        return pd.DataFrame()
    rows = []
    for item in arr:
        try:
            rows.append({
                "date": item[0],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "vol": float(item[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def year_volatility(ts_code: str, year: int) -> tuple[float | None, float | None]:
    """返回 (年末收盘价, 年化波动率 %); 无法获取时返回 (None, None)。

    波动率 = 日收益率标准差 × sqrt(252) × 100 (不复权收盘价, 与 A 股口径一致)。
    """
    df = _tencent_kline_df(ts_code)
    if df.empty:
        return None, None
    dfy = df[(df["date"].dt.year == year)].copy()
    if dfy.empty:
        return None, None
    closes = dfy["close"].dropna()
    if closes.empty:
        return None, None
    year_end_close = float(closes.iloc[-1])
    rets = closes.pct_change().dropna()
    if len(rets) < 2:
        return year_end_close, None
    vol = float(rets.std(ddof=1)) * ANNUALIZATION * 100.0
    return year_end_close, vol


def latest_close(ts_code: str) -> float | None:
    """最近一个交易日收盘价 (股息率-TTM 分母)。"""
    df = _tencent_kline_df(ts_code)
    if df.empty:
        return None
    return float(df["close"].iloc[-1])


def year_end_close(ts_code: str, year: int) -> float | None:
    """某年最后一个交易日收盘价 (静态股息率分母)。"""
    df = _tencent_kline_df(ts_code)
    if df.empty:
        return None
    dfy = df[df["date"].dt.year == year]
    if dfy.empty:
        return None
    return float(dfy["close"].iloc[-1])


def avg_daily_amt_wan(ts_code: str, year: int) -> float | None:
    """某年日均成交金额 (万港元), 近似 = mean(成交量(股) × 收盘价) / 10000。"""
    df = _tencent_kline_df(ts_code)
    if df.empty:
        return None
    dfy = df[df["date"].dt.year == year].copy()
    if dfy.empty:
        return None
    amt = (dfy["vol"] * dfy["close"]).dropna()
    if amt.empty:
        return None
    return float(amt.mean() / 10000.0)


# ---------------------------------------------------------------------------
# 便捷: 单只股票全部指标
# ---------------------------------------------------------------------------

def stock_metrics(ts_code: str, industry_map_: dict[str, str] | None = None) -> dict:
    """汇总单只港股的全部原始数据 (供红利低波/基本面计算):

    Returns:
      {
        "ts_code", "symbol", "name", "market", "industry",
        "fina": {year: {...}}, "dividends": {year: dps},
        "balance": {year: {...}}, "last_close": float
      }
    """
    stocks = hk_stock_list()
    hit = stocks[stocks["ts_code"].astype(str) == ts_code]
    if hit.empty:
        raise ValueError(f"未找到港股 {ts_code}")
    row = hit.iloc[0]
    name = str(row.get("name") or "")
    market = str(row.get("market") or "")
    ind = ""
    if industry_map_ is not None:
        ind = industry_map_.get(ts_code, "")
    return {
        "ts_code": ts_code,
        "symbol": ts_code.split(".")[0],
        "name": name,
        "market": market,
        "industry": ind,
        "fina": _fina_indicator_map(ts_code),
        "dividends": _dividend_map(ts_code),
        "balance": _balance_map(ts_code),
        "last_close": latest_close(ts_code),
    }
