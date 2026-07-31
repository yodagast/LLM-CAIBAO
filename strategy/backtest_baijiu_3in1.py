#!/usr/bin/env python3
"""
行业 全行业三合一策略回测 + 曲线绘制。

基于 backtest_hybrid_3in1.py 的三合一思路, 应用到指定行业全部上市公司:

  每只股票投入 30,000 元, 等分为 3 份 (各 10,000 元), 每份独立运行:

    策略 A (买入并持有): 开盘即买入, 一直持有
    策略 B (区间交易):   价格 ≤ 买入价 买入全部; 价格 ≥ 卖出价 或 价格 ≤ 止损价 卖出全部
    策略 C (低价突破):   买入: N日最低 且 价格 ≤ 买入价
                         卖出: 价格 ≥ 卖出价 或 (涨幅>阈值) 或 价格 ≤ 止损价

价格参数基于 2026-07-30 收盘价 (ref_close) 动态生成:
    买入价  = ref_close
    卖出价  = ref_close * 1.3
    止损价  = ref_close * 0.8

股票: 指定行业全部上市公司, 通过 tushare 自动获取 (东财行业分类)。
      支持命令行指定行业:  python backtest_baijiu_3in1.py --industry 银行
      常用行业名: 白酒(19) 银行(42) 煤炭开采(25) 机场(5) 保险(5) 证券(50) 等
"""

import argparse
import os
from collections import deque, defaultdict
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import tushare as ts

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

SUB_CASH = 10_000.0          # 每份资金
GAIN_THRESHOLD = 20.0        # 策略C 涨幅阈值 (%)
START_DATE = "20170101"
END_DATE = "20260730"        # 数据截至 2026-07-30
REF_DATE = "20260730"        # 参考日期: 用于计算买入/卖出/止损价
SELL_MULT = 1.3              # 卖出价 = 参考收盘价 * 1.3
STOP_MULT = 0.8              # 止损价 = 参考收盘价 * 0.8
N_VALUES = [5, 20, 60]

INDUSTRY = "化工原料"  # 回测行业: 白酒/银行/煤炭开采 等 (可用 --industry 覆盖)

# 股票配置: 运行时由 build_stock_configs() 自动填充指定行业全部股票
STOCK_CONFIGS: dict = {}

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def _csv_path() -> str:
    return os.path.join(OUTPUT_DIR, f"backtest_{INDUSTRY}_3in1.csv")


def _plot_path() -> str:
    return os.path.join(OUTPUT_DIR, f"backtest_{INDUSTRY}_3in1.png")


# ---------------------------------------------------------------------------
# Token / Data
# ---------------------------------------------------------------------------

def _load_env_token(env_path: str = "../.env") -> None:
    if os.getenv("TUSHARE_TOKEN"):
        return
    resolved = os.path.join(os.path.dirname(os.path.abspath(__file__)), env_path)
    if os.path.exists(resolved):
        with open(resolved) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k] = v


def _init_pro() -> ts.pro_api:
    _load_env_token()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置 TUSHARE_TOKEN。")
    ts.set_token(token)
    return ts.pro_api(token)


DATA_CACHE: dict[str, pd.DataFrame] = {}

def get_daily(ts_code: str, is_etf: bool) -> pd.DataFrame:
    if ts_code in DATA_CACHE:
        return DATA_CACHE[ts_code]
    pro = _init_pro()
    if is_etf:
        df = pro.fund_daily(ts_code=ts_code, start_date=START_DATE, end_date=END_DATE)
    else:
        df = pro.daily(ts_code=ts_code, start_date=START_DATE, end_date=END_DATE)
    if df.empty:
        raise ValueError(f"未获取到 {ts_code} 的日线数据。")
    df = df.sort_values("trade_date").reset_index(drop=True)
    DATA_CACHE[ts_code] = df
    return df


def build_stock_configs() -> dict:
    """
    获取指定行业全部股票, 并基于 REF_DATE 收盘价生成买入/卖出/止损价。

    行业匹配策略 (东财行业分类):
      1. 精确匹配 INDUSTRY (如 白酒/银行/机场)
      2. 若精确匹配为空, 则按子串匹配 (如 煤炭 → 煤炭开采)
      3. 仍为空则报错并列出可用行业。
    """
    pro = _init_pro()
    sb = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,industry")

    exact = sb[sb["industry"] == INDUSTRY]
    if exact.empty:
        fuzzy = sb[sb["industry"].str.contains(INDUSTRY, na=False)]
        if fuzzy.empty:
            avail = "、".join(sorted(sb["industry"].dropna().unique().tolist()))
            raise RuntimeError(f"行业 [{INDUSTRY}] 未找到对应股票。可用行业名示例:\n{avail}")
        print(f"  行业 [{INDUSTRY}] 精确匹配为空, 按子串匹配到: "
              f"{'、'.join(sorted(fuzzy['industry'].unique()))} ({len(fuzzy)} 只)")
        industry_df = fuzzy.sort_values("ts_code")
    else:
        print(f"  行业 [{INDUSTRY}] 精确匹配 {len(exact)} 只股票")
        industry_df = exact.sort_values("ts_code")

    configs: dict = {}
    for _, row in industry_df.iterrows():
        ts_code = row["ts_code"]
        try:
            df = get_daily(ts_code, is_etf=False)
        except Exception as e:
            print(f"  ! 获取 {ts_code}({row['name']}) 日线失败: {e}")
            continue

        ref = df[df["trade_date"] <= REF_DATE]
        if ref.empty:
            print(f"  ! {ts_code}({row['name']}) 在 {REF_DATE} 前无数据, 跳过")
            continue
        ref_close = float(ref.iloc[-1]["close"])

        configs[ts_code] = {
            "name": row["name"],
            "buy_max": ref_close,                       # 买入价
            "sell_floor": round(ref_close * SELL_MULT, 4),  # 卖出价
            "band_buy": ref_close,                      # 区间交易买入价
            "band_sell": round(ref_close * SELL_MULT, 4),   # 区间交易卖出价
            "stop_loss": round(ref_close * STOP_MULT, 4),   # 止损价
            "is_etf": False,
        }
    return configs


# ---------------------------------------------------------------------------
# 三个子策略
# ---------------------------------------------------------------------------

def _strat_buy_hold(df: pd.DataFrame, cash: float) -> list[dict]:
    """策略A: 买入并持有。"""
    close0 = float(df["close"].iloc[0])
    shares = cash / close0
    daily = []
    for _, row in df.iterrows():
        daily.append({"date": row["trade_date"], "value": shares * float(row["close"])})
    return daily


def _strat_band_trade(df: pd.DataFrame, cash: float, buy_price: float,
                      sell_price: float, stop_loss: float) -> list[dict]:
    """
    策略B: 区间交易 — 价格 ≤ buy_price 买入全部;
                     价格 ≥ sell_price 卖出全部;
                     价格 ≤ stop_loss 止损卖出。
    """
    shares = 0.0
    daily = []
    bought = False
    for _, row in df.iterrows():
        close = float(row["close"])
        if not bought and close <= buy_price:
            shares = cash / close
            cash = 0.0
            bought = True
        if bought and close >= sell_price:
            cash = shares * close
            shares = 0.0
            bought = False
        elif bought and close <= stop_loss:
            cash = shares * close
            shares = 0.0
            bought = False
        daily.append({"date": row["trade_date"], "value": cash + shares * close})
    return daily


def _strat_low_price(
    df: pd.DataFrame, cash: float,
    lookback_days: int, buy_max: float, sell_floor: float, stop_loss: float,
) -> list[dict]:
    """
    策略C: 低价突破策略。

    买入: 当日收盘价为 N 日最低价 且 价格 ≤ buy_max (买入价)
    卖出条件 (任一):
      A. 当日收盘价 ≥ sell_floor (卖出价)
      B. 持仓涨幅 > GAIN_THRESHOLD
      C. 当日收盘价 ≤ stop_loss (止损价)
    """
    shares = 0.0
    total_cost = 0.0
    window: deque[float] = deque(maxlen=lookback_days)
    daily = []

    for _, row in df.iterrows():
        date = row["trade_date"]
        close = float(row["close"])
        window.append(close)

        buy_signal = sell_signal = False
        sell_amount = 0.0

        if len(window) == lookback_days:
            n_day_low = min(window)

            # --- 买入: N日最低 + 价格低于限价 ---
            if close == n_day_low and close <= buy_max and cash >= SUB_CASH:
                buy_signal = True

            # --- 卖出 ---
            if shares > 0:
                if total_cost > 0:
                    gain_pct = (shares * close - total_cost) / total_cost * 100
                else:
                    gain_pct = 0.0

                cond_target = (close >= sell_floor)   # 达到卖出价
                cond_gain = (gain_pct > GAIN_THRESHOLD)
                cond_stop = (close <= stop_loss)      # 触发止损

                if cond_target or cond_gain or cond_stop:
                    sell_amount = shares * close
                    sell_signal = True

        if buy_signal:
            shares = SUB_CASH / close
            cash -= SUB_CASH
            total_cost = SUB_CASH
        elif sell_signal:
            cash += sell_amount
            shares = 0.0
            total_cost = 0.0

        daily.append({"date": date, "value": cash + shares * close})

    return daily


# ---------------------------------------------------------------------------
# 单股票三分组合
# ---------------------------------------------------------------------------

def run_one_stock_hybrid(
    ts_code: str, cfg: dict, lookback_days: int,
) -> tuple[list[dict], dict]:
    """运行单只股票的 3 份策略, 返回 (每日净值列表, {统计信息})。"""
    df = get_daily(ts_code, cfg["is_etf"])
    init_per_stock = SUB_CASH * 3  # 30,000

    a = _strat_buy_hold(df, SUB_CASH)
    b = _strat_band_trade(df, SUB_CASH, cfg["band_buy"], cfg["band_sell"], cfg["stop_loss"])
    c = _strat_low_price(df, SUB_CASH, lookback_days, cfg["buy_max"], cfg["sell_floor"], cfg["stop_loss"])

    # 合并三份
    merged: dict[str, float] = defaultdict(float)
    for sub in [a, b, c]:
        for dv in sub:
            merged[dv["date"]] += dv["value"]

    daily = [{"date": d, "value": v} for d, v in sorted(merged.items())]

    # 计算单只股票统计 (复用 _calc_result)
    stock_result = _calc_result(daily, cfg["name"], total_init=init_per_stock)
    total_ret = stock_result["ret"]
    ann_ret = stock_result["ann"]
    mdd = stock_result["mdd"]
    calmar = stock_result["calmar"]
    sharpe = stock_result["sharpe"]
    final_val = stock_result["portfolio"][-1]["value"] if stock_result["portfolio"] else init_per_stock

    # 子策略单独统计
    sub_stats = []
    for label, sub in [("买入持有", a), ("区间交易", b), ("低价突破", c)]:
        sv = sub[-1]["value"] if sub else 0
        sr = (sv / SUB_CASH - 1) * 100
        sub_stats.append({"子策略": label, "终值": sv, "收益": sr})

    stats = {
        "name": cfg["name"],
        "ts_code": ts_code,
        "买入价": cfg["buy_max"],
        "卖出价": cfg["sell_floor"],
        "止损价": cfg["stop_loss"],
        "总收益率": total_ret,
        "年化收益率": ann_ret,
        "最大回撤": mdd,
        "卡玛比率": calmar,
        "夏普比率": sharpe,
        "最终资产": final_val,
        "子策略": sub_stats,
    }
    return daily, stats


# ---------------------------------------------------------------------------
# 组合回测
# ---------------------------------------------------------------------------

def run_hybrid_portfolio(lookback_days: int) -> dict:
    """运行所有股票的 3 份策略, 返回组合结果及各股票明细。"""
    all_merged: dict[str, float] = defaultdict(float)
    stock_details: list[dict] = []

    for ts_code, cfg in STOCK_CONFIGS.items():
        daily, stats = run_one_stock_hybrid(ts_code, cfg, lookback_days)
        stock_details.append(stats)
        for dv in daily:
            all_merged[dv["date"]] += dv["value"]

    portfolio = [{"date": d, "value": v} for d, v in sorted(all_merged.items())]
    result = _calc_result(portfolio, f"{INDUSTRY}三合一 N={lookback_days}")
    result["stock_details"] = stock_details
    return result


def run_buy_hold_portfolio() -> dict:
    """对照组: 全部资金买入并持有。"""
    total_init = len(STOCK_CONFIGS) * SUB_CASH * 3
    merged: dict[str, float] = defaultdict(float)
    for ts_code, cfg in STOCK_CONFIGS.items():
        df = get_daily(ts_code, cfg["is_etf"])
        first_close = float(df["close"].iloc[0])
        shares = (SUB_CASH * 3) / first_close
        for _, row in df.iterrows():
            merged[row["trade_date"]] += shares * float(row["close"])
    portfolio = [{"date": d, "value": v} for d, v in sorted(merged.items())]
    return _calc_result(portfolio, "全部买入持有")


def run_band_only_portfolio() -> dict:
    """对照组: 全部资金做区间交易 (含止损)。"""
    merged: dict[str, float] = defaultdict(float)
    for ts_code, cfg in STOCK_CONFIGS.items():
        df = get_daily(ts_code, cfg["is_etf"])
        sub = _strat_band_trade(df, SUB_CASH * 3, cfg["band_buy"], cfg["band_sell"], cfg["stop_loss"])
        for dv in sub:
            merged[dv["date"]] += dv["value"]
    portfolio = [{"date": d, "value": v} for d, v in sorted(merged.items())]
    return _calc_result(portfolio, "全部区间交易")


def _calc_result(portfolio: list[dict], label: str, total_init: float | None = None) -> dict:
    """
    计算收益率、年化、最大回撤、卡玛比率、夏普比率。

    Parameters
    ----------
    total_init : float | None
        初始总资金。为 None 时自动用全局配置计算。
    """
    if total_init is None:
        total_init = len(STOCK_CONFIGS) * SUB_CASH * 3
    if not portfolio:
        return {"label": label, "portfolio": [], "ret": 0, "ann": 0, "mdd": 0,
                "calmar": 0.0, "sharpe": 0.0}
    final = portfolio[-1]["value"]

    years = (datetime.strptime(END_DATE, "%Y%m%d") -
             datetime.strptime(START_DATE, "%Y%m%d")).days / 365.25
    # 总收益率 (百分比)
    ret = (final / total_init - 1) * 100
    # 年化收益率 (百分比)
    ann = (pow(final / total_init, 1 / years) - 1) * 100 if years > 0 else 0.0

    # --- 最大回撤 ---
    peak = total_init
    mdd = 0.0
    for dv in portfolio:
        v = dv["value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd:
            mdd = dd

    # --- 卡玛比率 = 年化收益率 / 最大回撤 (绝对值) ---
    calmar = ann / mdd if mdd > 0 else 0.0

    # --- 夏普比率 = (年化收益率 - 无风险利率) / 年化波动率 ---
    RISK_FREE_RATE = 0.0  # 简化处理，无风险利率设为 0%
    # 计算每日收益率
    values = [dv["value"] for dv in portfolio]
    daily_returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            daily_returns.append(values[i] / values[i - 1] - 1)
    if daily_returns:
        import numpy as np
        ann_vol = np.std(daily_returns, ddof=1) * np.sqrt(252)  # 年化波动率
        # 用小数形式计算夏普
        sharpe = ((ann - RISK_FREE_RATE) / 100) / ann_vol if ann_vol > 0 else 0.0
    else:
        sharpe = 0.0

    return {"label": label, "portfolio": portfolio, "ret": ret, "ann": ann, "mdd": mdd,
            "calmar": calmar, "sharpe": sharpe}


# ---------------------------------------------------------------------------
# 绘图 + 输出
# ---------------------------------------------------------------------------

def _fmt_pct(x, _):
    return f"{x:.0f}%"


def plot_and_print(results: list[dict], benchmarks: list[dict]) -> None:
    """打印表格 + 绘制曲线。"""
    sep = "=" * 72
    total_init = len(STOCK_CONFIGS) * SUB_CASH * 3

    # ====== 组合总览 ======
    print(f"\n{sep}")
    print(f"  {INDUSTRY}行业三合一策略回测对比 (合计初始: {total_init:,.0f} 元, 共 {len(STOCK_CONFIGS)} 只股票)")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}  参考价: {REF_DATE} 收盘")
    print(f"  价格参数: 买入价=参考收盘 卖出价=参考收盘x{SELL_MULT} 止损价=参考收盘x{STOP_MULT}")
    print(f"{sep}")
    print(f"  {'策略':<28} {'总收益率':>10} {'年化':>8} {'回撤':>8} {'卡玛':>7} {'夏普':>7}")
    print(f"  {'-' * 68}")

    for r in results:
        print(f"  {r['label']:<28} {r['ret']:>+9.2f}% {r['ann']:>+7.2f}% {r['mdd']:>7.2f}%"
              f" {r['calmar']:>6.2f} {r['sharpe']:>6.2f}")

    print(f"  {'-' * 68}")
    for b in benchmarks:
        print(f"  {b['label']:<28} {b['ret']:>+9.2f}% {b['ann']:>+7.2f}% {b['mdd']:>7.2f}%"
              f" {b['calmar']:>6.2f} {b['sharpe']:>6.2f}")
    print(f"{sep}")

    # ====== 每只股票明细 (取第一个 N 的结果展示) ======
    detail = results[0]
    print(f"\n  ── {detail['label']} 各股票明细 ──")
    print(f"  {'股票':<16} {'买入价':>8} {'卖出价':>8} {'止损价':>8}"
          f" {'总收益':>9} {'年化':>8} {'回撤':>8} {'卡玛':>7} {'夏普':>7} {'终值':>10}")
    print(f"  {'-' * 96}")
    for s in detail.get("stock_details", []):
        print(f"  {s['name']+'('+s['ts_code'][:6]+')':<16}"
              f" {s['买入价']:>7.2f} {s['卖出价']:>7.2f} {s['止损价']:>7.2f}"
              f" {s['总收益率']:>+8.2f}% {s['年化收益率']:>+7.2f}% {s['最大回撤']:>7.2f}%"
              f" {s['卡玛比率']:>6.2f} {s['夏普比率']:>6.2f} {s['最终资产']:>9,.0f}")
    # 子策略明细
    for s in detail.get("stock_details", []):
        print(f"    ├ {s['name']} 子策略:")
        for sub in s["子策略"]:
            print(f"    │  ├ {sub['子策略']:8s} 终值 {sub['终值']:>7,.0f}  收益 {sub['收益']:>+6.2f}%")

    # CSV
    rows = []
    for r in results + benchmarks:
        rows.append({"策略": r["label"], "总收益率%": round(r["ret"], 2),
                     "年化收益率%": round(r["ann"], 2), "最大回撤%": round(r["mdd"], 2),
                     "卡玛比率": round(r["calmar"], 2), "夏普比率": round(r["sharpe"], 2)})
    for s in detail.get("stock_details", []):
        rows.append({"策略": s["name"] + "(" + s["ts_code"][:6] + ")",
                     "买入价": s["买入价"], "卖出价": s["卖出价"], "止损价": s["止损价"],
                     "总收益率%": round(s["总收益率"], 2),
                     "年化收益率%": round(s["年化收益率"], 2),
                     "最大回撤%": round(s["最大回撤"], 2),
                     "卡玛比率": round(s["卡玛比率"], 2),
                     "夏普比率": round(s["夏普比率"], 2)})
    pd.DataFrame(rows).to_csv(_csv_path(), index=False, encoding="utf-8-sig")
    print(f"\n[CSV] {_csv_path()}")

    # ---- 绘图 ----
    if not HAVE_MPL:
        print("[跳过] matplotlib 不可用。")
        return

    import matplotlib.font_manager as fm
    candidate_fonts = [
        "PingFang HK", "PingFang SC", "Heiti TC", "Heiti SC",
        "Songti SC", "Hiragino Sans GB", "Hiragino Sans",
        "Source Han Sans CN", "Apple SD Gothic Neo",
        "WenQuanYi Micro Hei", "Noto Sans CJK SC", "SimHei",
    ]
    chosen = None
    for font_name in candidate_fonts:
        try:
            fp = fm.findfont(font_name, fallback_to_default=False)
            if "DejaVuSans" in fp:
                continue
            chosen = font_name
            break
        except Exception:
            continue
    if chosen is None:
        chosen = "DejaVu Sans"
    plt.rcParams["font.sans-serif"] = [chosen]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(14, 7))

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    for i, r in enumerate(results):
        p = [(dv["value"] / total_init - 1) * 100 for dv in r["portfolio"]]
        dates = [dv["date"] for dv in r["portfolio"]]
        ax.plot(dates, p, label=r["label"], color=colors[i % len(colors)],
                linewidth=1.5, alpha=0.9)

    # 对照组虚线
    for b in benchmarks:
        p = [(dv["value"] / total_init - 1) * 100 for dv in b["portfolio"]]
        dates = [dv["date"] for dv in b["portfolio"]]
        ls = "--" if "买入持有" in b["label"] else ":"
        ax.plot(dates, p, label=b["label"], color="#888888", linewidth=2.5, linestyle=ls, alpha=0.8)

    stocks_str = "+".join(c["name"] for c in STOCK_CONFIGS.values())
    ax.set_title(f"{INDUSTRY}行业三合一策略回测对比 ({len(STOCK_CONFIGS)}只, 买入=收盘 卖出=1.3x 止损=0.8x, 各3万等分3份)",
                 fontsize=13, fontweight="bold")
    ax.set_ylabel("累计收益率 (%)", fontsize=12)
    ax.set_xlabel("日期", fontsize=12)
    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_pct))
    ax.axhline(y=0, color="gray", linewidth=0.5)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(_plot_path(), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"[图表] {_plot_path()}")


def main() -> None:
    global STOCK_CONFIGS, INDUSTRY

    parser = argparse.ArgumentParser(description="行业三合一策略回测 (买入=参考收盘 卖出=1.3x 止损=0.8x)")
    parser.add_argument("--industry", default=INDUSTRY,
                        help="回测行业名称, 如: 白酒/银行/煤炭/机场/保险/证券 等 (默认: 白酒)")
    args = parser.parse_args()
    INDUSTRY = args.industry

    print(f"正在获取 [{INDUSTRY}] 行业股票列表...")
    STOCK_CONFIGS = build_stock_configs()
    if not STOCK_CONFIGS:
        raise RuntimeError(f"未获取到 [{INDUSTRY}] 行业股票, 请检查行业名称 / tushare token / 网络。")
    print(f"  共 {len(STOCK_CONFIGS)} 只 {INDUSTRY} 股参与回测:")
    for ts_code, cfg in STOCK_CONFIGS.items():
        print(f"    ✓ {cfg['name']}({ts_code}) 买{cfg['buy_max']:.2f} 卖{cfg['sell_floor']:.2f} 损{cfg['stop_loss']:.2f}")

    print("\n正在运行回测...")

    results = []
    for n in N_VALUES:
        r = run_hybrid_portfolio(lookback_days=n)
        results.append(r)
        print(f"  ✓ {INDUSTRY}三合一 N={n}")

    bh = run_buy_hold_portfolio()
    band = run_band_only_portfolio()
    print("  ✓ 全部买入持有")
    print("  ✓ 全部区间交易")

    plot_and_print(results, [bh, band])


if __name__ == "__main__":
    main()
