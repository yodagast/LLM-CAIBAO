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


def search_industries(keyword: str = "", limit: int = 20) -> list[dict]:
    """行业模糊搜索: 返回匹配的行业及所含股票数量 (供前端候选推荐)。

    按关键词子串匹配行业名, 无关键词时返回股票数最多的热门行业;
    结果按股票数降序。
    """
    stocks = _stock_basic()
    counts = stocks["industry"].value_counts()
    keyword = (keyword or "").strip()
    if keyword:
        mask = counts.index.str.contains(keyword, na=False)
        counts = counts[mask]
    items = [
        {"industry": str(ind), "count": int(c)}
        for ind, c in counts.head(limit).items()
    ]
    return items


# ---------------------------------------------------------------------------
# 日线数据 (带缓存)
# ---------------------------------------------------------------------------

_DAILY_CACHE: dict[str, pd.DataFrame] = {}


def _adj_close(df: pd.DataFrame) -> pd.Series:
    """用 pct_chg 重建前复权连续收盘价, 消除除权除息/份额拆分/分红跳空。

    tushare 的 daily/fund_daily 返回不复权价格, 遇拆分/分红时 close 会跳空
    (如 512170 2021-02-25 份额拆分 close 2.564→0.818), 直接算净值/收益会产生
    假暴跌。而 pct_chg 字段是按拆分/分红调整后的正确涨跌幅 (拆分日 pre_close 已
    同步调整), 从最新价向前累计即可得到连续的前复权价 (最新价=真实价)。
    """
    close = df["close"].astype(float)
    pct = df["pct_chg"].astype(float)
    n = len(df)
    out = close.to_numpy().copy()
    for i in range(n - 2, -1, -1):
        p = pct.iloc[i + 1]
        if pd.notna(p):
            out[i] = out[i + 1] / (1 + p / 100.0)
        elif close.iloc[i + 1] > 0:
            out[i] = out[i + 1] * (close.iloc[i] / close.iloc[i + 1])
        else:
            out[i] = out[i + 1]
    return pd.Series(out, index=df.index)


def get_daily(ts_code: str, kind: str = "stock",
              start_date: str = "20170101", end_date: str = "",
              adj: str = "") -> pd.DataFrame:
    """获取单只股票/ETF 日线 (trade_date 升序), 带内存缓存。

    adj: "" 不复权(原始价格) / "qfq" 前复权 (用 pct_chg 重建连续价, 消除拆分/分红跳空)。
    回测/收益计算建议用 "qfq"; 行情展示用默认不复权。
    """
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    adj_key = str(adj).strip().lower()
    cache_key = f"{ts_code}:{kind}:{start_date}:{end_date}:{adj_key}"
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
    if adj_key == "qfq":
        df = df.copy()
        df["close"] = _adj_close(df)
        df["pre_close"] = df["close"].shift(1).fillna(df["close"])
    _DAILY_CACHE[cache_key] = df
    return df


def get_quote(ts_code: str, kind: str = "stock", days: int = 120) -> pd.DataFrame:
    """获取最近 N 个交易日的行情数据 (升序), 供前端绘制行情曲线。"""
    end_date = datetime.now().strftime("%Y%m%d")
    # 按约 2.5 倍自然日预取, 以覆盖 N 个交易日
    start = (datetime.now() - timedelta(days=int(days * 2.5) + 30)).strftime("%Y%m%d")
    df = get_daily(ts_code, kind, start_date=start, end_date=end_date)
    return df.tail(days).reset_index(drop=True)


def get_kline(ts_code: str, kind: str = "stock", freq: str = "D", adj: str = "",
              start_date: str = "", end_date: str = "", hist_years: int = 20) -> list[dict]:
    """获取 K 线 bars (周期: D日/W周/M月; 复权: qfq前复权/hfq后复权/空不复权)。

    基于 tushare pro_bar 接口。返回升序 bars (含 open/high/low/close/pre_close/change/pct_chg/vol/amount/amplitude)。
    D 线额外附带 turnover_rate (换手率, 来自 daily_basic); W/M 线换手率为空。
    """
    end = end_date or datetime.now().strftime("%Y%m%d")
    start = start_date or (datetime.now() - timedelta(days=int(hist_years * 365.25) + 10)).strftime("%Y%m%d")
    adj_param = adj.strip() or None
    try:
        df = ts.pro_bar(ts_code=ts_code, freq=freq.upper(), adj=adj_param,
                        start_date=start, end_date=end)
    except Exception as e:
        raise ValueError(f"获取 {ts_code} {freq}线{adj or '不复权'}数据失败: {e}")
    if df is None or df.empty:
        raise ValueError(f"未获取到 {ts_code} {freq}线 {adj or '不复权'} 数据")
    df = df.sort_values("trade_date").reset_index(drop=True)

    # 日线附带回换手率 (换手率不随复权改变, 仅日线粒度有)
    turnover_map = {}
    if freq.upper() == "D" and kind != "fund":
        try:
            pro = _init_pro()
            tb = pro.daily_basic(ts_code=ts_code, start_date=start, end_date=end,
                                 fields="trade_date,turnover_rate")
            if tb is not None and not tb.empty:
                turnover_map = {
                    str(r["trade_date"]): _to_float(r.get("turnover_rate"))
                    for _, r in tb.iterrows()
                }
        except Exception:
            turnover_map = {}

    bars = []
    for _, row in df.iterrows():
        td = str(row["trade_date"])
        pre = _to_float(row.get("pre_close"))
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if (pre and pre > 0) else None
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": float(row["close"]),
            "pre_close": pre,
            "change": _to_float(row.get("change")),
            "pct_chg": float(row.get("pct_chg", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": turnover_map.get(td),
        })
    return bars


def get_stock_detail(ts_code: str, kind: str = "stock", days: int = 250,
                     hist_years: int = 20, date: str = "") -> dict:
    """股票详情聚合: 最多 hist_years 年 K 线 + 52 周高低 + PB/PE/股本/市值 + 分红/股息率。

    date: 指定交易日 (YYYYMMDD) 查看该日行情快照, 空=最新交易日。
    """
    pro = _init_pro()
    end_date = datetime.now().strftime("%Y%m%d")
    # K 线历史: 最多 hist_years 年 (约 250 交易日/年)
    start = (datetime.now() - timedelta(days=int(hist_years * 365.25) + 10)).strftime("%Y%m%d")
    df = get_daily(ts_code, kind, start_date=start, end_date=end_date)

    # 选中目标交易日 (指定日期或最新)
    if date:
        target = df[df["trade_date"].astype(str) == str(date)]
        if target.empty:
            raise ValueError(f"交易日 {date} 无行情数据 (可能停牌或未上市)")
        quote_row = target.iloc[0]
    else:
        quote_row = df.iloc[-1]

    recent = df.tail(days).reset_index(drop=True)  # 52 周窗口 (用于高低/最新价)
    last_close = float(quote_row["close"])
    last_date = str(quote_row["trade_date"])
    week52_high = float(recent["high"].max())
    week52_low = float(recent["low"].min())

    # 昨收: 优先 daily.pre_close, 否则取前一交易日收盘
    pre_close = _to_float(quote_row.get("pre_close"))
    if pre_close is None:
        dates = df["trade_date"].astype(str).tolist()
        pos = dates.index(last_date) if last_date in dates else -1
        if pos > 0:
            pre_close = _to_float(df.iloc[pos - 1].get("close"))

    # 当日行情快照 (成交量单位:手, 成交额单位:千元; 盘后成交量 tushare 无此字段; 振幅=(最高-最低)/昨收)
    q_hi = _to_float(quote_row.get("high"))
    q_lo = _to_float(quote_row.get("low"))
    amp_quote = None
    if pre_close and pre_close > 0 and q_hi is not None and q_lo is not None:
        amp_quote = (q_hi - q_lo) / pre_close * 100.0
    quote = {
        "trade_date": last_date,
        "open": _to_float(quote_row.get("open")),
        "high": q_hi,
        "low": q_lo,
        "close": _to_float(quote_row.get("close")),
        "pre_close": pre_close,
        "change": _to_float(quote_row.get("change")),
        "pct_chg": _to_float(quote_row.get("pct_chg")),
        "vol": _to_float(quote_row.get("vol")),
        "amount": _to_float(quote_row.get("amount")),
        "amplitude": amp_quote,
        "after_vol": None,
    }

    # 最新 daily_basic: PB/PE/股本/市值/股息率 + 全历史逐日换手率 (供日期切换取当日换手率)
    pb = pe = pe_ttm = total_share = float_share = total_mv = circ_mv = dv_ratio = None
    turnover_map = {}
    try:
        b = pro.daily_basic(
            ts_code=ts_code, start_date=start, end_date=end_date,
            fields="trade_date,close,pb,pe,pe_ttm,total_share,float_share,total_mv,circ_mv,dv_ratio,dv_ttm,turnover_rate",
        )
        if b is not None and not b.empty:
            b = b.sort_values("trade_date").reset_index(drop=True)
            turnover_map = {
                str(r["trade_date"]): _to_float(r.get("turnover_rate"))
                for _, r in b.iterrows()
            }
            latest = b.iloc[-1]
            pb = _to_float(latest.get("pb"))
            pe = _to_float(latest.get("pe"))
            pe_ttm = _to_float(latest.get("pe_ttm"))
            total_share = _to_float(latest.get("total_share"))   # 万股
            float_share = _to_float(latest.get("float_share"))   # 万股
            total_mv = _to_float(latest.get("total_mv"))         # 万元
            circ_mv = _to_float(latest.get("circ_mv"))           # 万元
            dv_ratio = _to_float(latest.get("dv_ratio"))
    except Exception:
        pass

    # 当日换手率 (来自 daily_basic)
    quote["turnover_rate"] = turnover_map.get(last_date)

    # 分红: 最新分红年度全部已实施每股现金红利之和 (一年多次分红求和)
    div = _dividend_latest(pro, ts_code)
    div_per_share = div["cash_div"] if div else None
    dividend_end = div["end_date"] if div else ""

    # 股息率: 优先 daily_basic.dv_ratio, 否则 每股分红/最新价
    dividend_yield = dv_ratio
    if div_per_share is not None and last_close:
        dividend_yield = div_per_share / last_close * 100.0

    # K 线 bars (全部历史, 供前端缩放查看最多 20 年; 含快照字段供日期切换本地取用)
    bars = []
    for _, row in df.iterrows():
        td = str(row["trade_date"])
        pre = _to_float(row.get("pre_close"))
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if (pre and pre > 0) else None
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": float(row["close"]),
            "pre_close": pre,
            "change": _to_float(row.get("change")),
            "pct_chg": float(row.get("pct_chg", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": turnover_map.get(td),
        })

    return {
        "last_close": last_close,
        "last_date": last_date,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "hist_years": hist_years,
        "bars_count": len(bars),
        "quote": quote,
        "pb": pb,
        "pe": pe,
        "pe_ttm": pe_ttm,
        "total_share": total_share,
        "float_share": float_share,
        "total_mv": total_mv,
        "circ_mv": circ_mv,
        "dv_ratio": dv_ratio,
        "div_per_share": div_per_share,
        "dividend_end": dividend_end,
        "dividend_yield": dividend_yield,
        "bars": bars,
    }


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


def _annual_div_per_share(pro, ts_code: str, year: int) -> float | None:
    """某分红年度 (end_date 年份==year) 全部已实施每股现金红利之和 (元)。

    tushare dividend 的 end_date 可为年内各期 (0630 中期/0930 三季/1231 年度),
    同一方案又有 预案/股东大会/实施 多条流程记录。许多公司一年多次分红 (中期+
    末期等), 若只取单个 end_date 的单条'实施'记录会严重低估全年每股分红与股息率。
    故按 end_date 年份聚合所有'实施'记录 cash_div 求和; 无实施记录时退回该年
    cash_div 最大值。
    """
    try:
        dv = pro.dividend(ts_code=ts_code)
    except Exception:
        return None
    if dv is None or dv.empty:
        return None
    yearly = dv[dv["end_date"].astype(str).str.startswith(str(year))].copy()
    if yearly.empty or "cash_div" not in yearly.columns:
        return None
    yearly["_cash"] = pd.to_numeric(yearly["cash_div"], errors="coerce")
    impl = yearly[yearly["div_proc"] == "实施"]
    if not impl.empty:
        # tushare 偶发同一 end_date 有重复'实施'记录 (如茅台 20251231 两条相同
        # 28.02423), 按 (end_date, cash_div) 去重后再求和, 避免重复计入
        impl = impl.drop_duplicates(subset=["end_date", "cash_div"])
        total = float(impl["_cash"].sum(skipna=True))
        return total if total > 0 else None
    mx = float(yearly["_cash"].max(skipna=True))
    return mx if mx > 0 else None


def _dividend_latest(pro, ts_code: str) -> dict | None:
    """获取单只股票最近一个分红年度全部已实施每股现金红利之和 (元)。

    按 end_date 年份聚合'实施'记录求和 (解决一年多次分红低估), 取最近有实施记录
    的年份; 若无实施记录则退回全部记录中 cash_div 最大那条。
    返回 {"end_date": "YYYY1231", "cash_div": 全年每股现金红利}。
    """
    try:
        dv = pro.dividend(ts_code=ts_code)
    except Exception:
        return None
    if dv is None or dv.empty:
        return None
    dv = dv.copy()
    if "cash_div" not in dv.columns:
        return None
    dv["_cash"] = pd.to_numeric(dv["cash_div"], errors="coerce")
    dv["_yr"] = pd.to_numeric(dv["end_date"].astype(str).str[:4], errors="coerce")
    impl = dv[(dv["div_proc"] == "实施") & (dv["_yr"].notna())]
    if not impl.empty:
        # 同 end_date 重复记录 (tushare 偶发) 去重后再求和
        impl = impl.drop_duplicates(subset=["end_date", "cash_div"])
        latest_yr = int(impl["_yr"].max())
        sel = impl[impl["_yr"] == latest_yr]
        total = float(sel["_cash"].sum(skipna=True))
        return {"end_date": f"{latest_yr}1231", "cash_div": total if total > 0 else None}
    # 无实施记录: 退回现金红利最大的一条
    dv = dv.dropna(subset=["_cash"])
    if dv.empty:
        return None
    best = dv.loc[dv["_cash"].idxmax()]
    val = float(best["_cash"])
    return {"end_date": str(best.get("end_date") or ""), "cash_div": val if val > 0 else None}


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
