#!/usr/bin/env python3
"""
三分组合策略回测 + 曲线绘制。

每只股票投入 30,000 元, 等分为 3 份 (各 10,000 元), 每份独立运行:

  策略 A (买入并持有): 开盘即买入, 一直持有
  策略 B (区间交易):  低于限价买入全部, 高于限价卖出全部
  策略 C (低价突破):  买入: N日最低 + 价格低于买入限价
                      卖出: N日最高 或 (涨幅>20%且价格≥卖出底价)

股票: 五粮液(000858.SZ), 招商银行(600036.SH), 中概互联ETF(513050.SH)
"""

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
GAIN_THRESHOLD = 20.0        # 涨幅阈值 (%)
START_DATE = "20170101"
END_DATE = "20260728"
N_VALUES = [5, 20, 60, 250]

# 股票配置
STOCK_CONFIGS = {
    # "000858.SZ": {
    #     "name": "五粮液",
    #     "buy_max": 75.0,       # 买入限价
    #     "sell_floor": 65.0,    # 低价策略卖出底价
    #     "band_buy": 75.0,      # 区间交易买入价
    #     "band_sell": 150.0,    # 区间交易卖出价
    #     "is_etf": False,
    # },
    # "600036.SH": {
    #     "name": "招商银行",
    #     "buy_max": 30.0,
    #     "sell_floor": 26.0,    # 招行比例 ≈ 65/75*30
    #     "band_buy": 30.0,
    #     "band_sell": 40.0,
    #     "is_etf": False,
    # },
    "601166.SH": {
        "name": "兴业银行",
        "buy_max": 18.0,
        "sell_floor": 15.0,    # 招行比例 ≈ 65/75*30
        "band_buy": 18.0,
        "band_sell": 25.0,
        "is_etf": False,
    },
    "002142.SZ": {
        "name": "宁波银行",
        "buy_max": 24,
        "sell_floor": 32,    # 招行比例 ≈ 65/75*30
        "band_buy": 24,
        "band_sell": 32,
        "is_etf": False,
    },
    "601658.SH": {
        "name": "邮储银行",
        "buy_max": 4.6,
        "sell_floor": 4.2,    # 招行比例 ≈ 65/75*30
        "band_buy": 4.6,
        "band_sell": 6.0,
        "is_etf": False,
    },
    # "513050.SH": {
    #     "name": "中概互联ETF",
    #     "buy_max": 1.1,
    #     "sell_floor": 1.0,    # 招行比例 ≈ 65/75*30
    #     "band_buy": 1.1,
    #     "band_sell": 1.5,
    #     "is_etf": True,
    # },
    #  "510050.SH": {
    #     "name": "上证50ETF",
    #     "buy_max": 2.0,
    #     "sell_floor": 1.0,    # 招行比例 ≈ 65/75*30
    #     "band_buy": 1.1,
    #     "band_sell": 1.5,
    #     "is_etf": True,
    # },
    # "512500.SH": {
    #     "name": "中证500ETF",
    #     "buy_max": 1.1,
    #     "sell_floor": 1.0,    # 招行比例 ≈ 65/75*30
    #     "band_buy": 1.1,
    #     "band_sell": 1.5,
    #     "is_etf": True,
    # },
}

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
PLOT_PATH = os.path.join(OUTPUT_DIR, "backtest_hybrid_3in1.png")
CSV_PATH = os.path.join(OUTPUT_DIR, "backtest_hybrid_3in1.csv")


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


def _strat_band_trade(df: pd.DataFrame, cash: float, buy_price: float, sell_price: float) -> list[dict]:
    """策略B: 区间交易 — 低于 buy_price 买入全部, 高于 sell_price 卖出全部。"""
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
        daily.append({"date": row["trade_date"], "value": cash + shares * close})
    return daily


def _strat_low_price(
    df: pd.DataFrame, cash: float,
    lookback_days: int, buy_max: float, sell_floor: float,
) -> list[dict]:
    """
    策略C: 低价突破策略。

    买入: 当日收盘价为 N 日最低价 且 价格 ≤ buy_max
    卖出条件 (任一):
      A. 当日收盘价为 N 日最高价
      B. 持仓涨幅 > 20% 且 价格 ≥ sell_floor
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
            n_day_high = max(window)

            # --- 买入: N日最低 + 价格低于限价 ---
            if close == n_day_low and close <= buy_max and cash >= SUB_CASH:
                buy_signal = True

            # --- 卖出 ---
            if shares > 0:
                if total_cost > 0:
                    gain_pct = (shares * close - total_cost) / total_cost * 100
                else:
                    gain_pct = 0.0

                cond_nday_high = (close == n_day_high)
                cond_gain = (gain_pct > GAIN_THRESHOLD) and (close >= sell_floor)

                if cond_nday_high or cond_gain:
                    # 全部卖出
                    sell_amount = shares * close
                    # 至少保留 1 万底舱? 但总仓位才 1 万, 全部卖出
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
    b = _strat_band_trade(df, SUB_CASH, cfg["band_buy"], cfg["band_sell"])
    c = _strat_low_price(df, SUB_CASH, lookback_days, cfg["buy_max"], cfg["sell_floor"])

    # 合并三份
    merged: dict[str, float] = defaultdict(float)
    for sub in [a, b, c]:
        for dv in sub:
            merged[dv["date"]] += dv["value"]

    daily = [{"date": d, "value": v} for d, v in sorted(merged.items())]

    # 计算单只股票统计
    final_val = daily[-1]["value"] if daily else init_per_stock
    total_ret = (final_val / init_per_stock - 1) * 100
    years = (datetime.strptime(END_DATE, "%Y%m%d") -
             datetime.strptime(START_DATE, "%Y%m%d")).days / 365.25
    ann_ret = (pow(final_val / init_per_stock, 1 / years) - 1) * 100 if years > 0 else 0.0

    peak = init_per_stock
    mdd = 0.0
    for dv in daily:
        v = dv["value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd:
            mdd = dd

    # 子策略单独统计
    sub_stats = []
    for label, sub in [("买入持有", a), ("区间交易", b), ("低价突破", c)]:
        sv = sub[-1]["value"] if sub else 0
        sr = (sv / SUB_CASH - 1) * 100
        sub_stats.append({"子策略": label, "终值": sv, "收益": sr})

    stats = {
        "name": cfg["name"],
        "ts_code": ts_code,
        "总收益率": total_ret,
        "年化收益率": ann_ret,
        "最大回撤": mdd,
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
    result = _calc_result(portfolio, f"三分组合 N={lookback_days}")
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
    """对照组: 全部资金做简单区间交易。"""
    merged: dict[str, float] = defaultdict(float)
    for ts_code, cfg in STOCK_CONFIGS.items():
        df = get_daily(ts_code, cfg["is_etf"])
        sub = _strat_band_trade(df, SUB_CASH * 3, cfg["band_buy"], cfg["band_sell"])
        for dv in sub:
            merged[dv["date"]] += dv["value"]
    portfolio = [{"date": d, "value": v} for d, v in sorted(merged.items())]
    return _calc_result(portfolio, "全部区间交易")


def _calc_result(portfolio: list[dict], label: str) -> dict:
    """计算收益率、年化、最大回撤。"""
    total_init = len(STOCK_CONFIGS) * SUB_CASH * 3
    if not portfolio:
        return {"label": label, "portfolio": [], "ret": 0, "ann": 0, "mdd": 0}
    final = portfolio[-1]["value"]

    years = (datetime.strptime(END_DATE, "%Y%m%d") -
             datetime.strptime(START_DATE, "%Y%m%d")).days / 365.25
    ret = (final / total_init - 1) * 100
    ann = (pow(final / total_init, 1 / years) - 1) * 100 if years > 0 else 0.0

    peak = total_init
    mdd = 0.0
    for dv in portfolio:
        v = dv["value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd:
            mdd = dd

    return {"label": label, "portfolio": portfolio, "ret": ret, "ann": ann, "mdd": mdd}


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
    print(f"  三分组合策略回测对比 (合计初始: {total_init:,.0f} 元)")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"  股票: {', '.join(c['name'] for c in STOCK_CONFIGS.values())}")
    print(f"{sep}")
    print(f"  {'策略':<28} {'总收益率':>10} {'年化':>8} {'最大回撤':>8}")
    print(f"  {'-' * 56}")

    for r in results:
        print(f"  {r['label']:<28} {r['ret']:>+9.2f}% {r['ann']:>+7.2f}% {r['mdd']:>7.2f}%")

    print(f"  {'-' * 56}")
    for b in benchmarks:
        print(f"  {b['label']:<28} {b['ret']:>+9.2f}% {b['ann']:>+7.2f}% {b['mdd']:>7.2f}%")
    print(f"{sep}")

    # ====== 每只股票明细 ======
    for r in results:
        print(f"\n  ── {r['label']} 各股票明细 ──")
        print(f"  {'股票':<14} {'总收益率':>10} {'年化':>8} {'回撤':>8} {'终值':>10}")
        print(f"  {'-' * 52}")
        for s in r.get("stock_details", []):
            print(f"  {s['name']+'('+s['ts_code'][:6]+')':<14} {s['总收益率']:>+9.2f}% {s['年化收益率']:>+7.2f}% {s['最大回撤']:>7.2f}% {s['最终资产']:>9,.0f}")
        # 子策略明细
        for s in r.get("stock_details", []):
            print(f"    ├ {s['name']} 子策略:")
            for sub in s["子策略"]:
                print(f"    │  ├ {sub['子策略']:8s} 终值 {sub['终值']:>7,.0f}  收益 {sub['收益']:>+6.2f}%")

    # CSV
    rows = []
    for r in results + benchmarks:
        rows.append({"策略": r["label"], "总收益率%": round(r["ret"], 2),
                     "年化收益率%": round(r["ann"], 2), "最大回撤%": round(r["mdd"], 2)})
    pd.DataFrame(rows).to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    print(f"\n[CSV] {CSV_PATH}")

    # ---- 绘图 ----
    if not HAVE_MPL:
        print("[跳过] matplotlib 不可用。")
        return

    for font_name in ["PingFang SC", "Heiti SC", "Apple SD Gothic Neo",
                       "WenQuanYi Micro Hei", "Noto Sans CJK SC", "SimHei", "DejaVu Sans"]:
        try:
            plt.rcParams["font.sans-serif"] = [font_name]
            plt.rcParams["axes.unicode_minus"] = False
            fig_t, ax_t = plt.subplots(figsize=(1, 1))
            ax_t.set_title("T")
            plt.close(fig_t)
            break
        except Exception:
            continue

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
    ax.set_title(f"三分组合策略回测对比 ({stocks_str}, 各3万等分3份)", fontsize=13, fontweight="bold")
    ax.set_ylabel("累计收益率 (%)", fontsize=12)
    ax.set_xlabel("日期", fontsize=12)
    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_pct))
    ax.axhline(y=0, color="gray", linewidth=0.5)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(PLOT_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"[图表] {PLOT_PATH}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    print("正在获取数据...")
    for ts_code, cfg in STOCK_CONFIGS.items():
        get_daily(ts_code, cfg["is_etf"])
        print(f"  ✓ {cfg['name']}({ts_code})")

    print("\n正在运行回测...")

    results = []
    for n in N_VALUES:
        r = run_hybrid_portfolio(lookback_days=n)
        results.append(r)
        print(f"  ✓ 三分组合 N={n}")

    bh = run_buy_hold_portfolio()
    band = run_band_only_portfolio()
    print(f"  ✓ 全部买入持有")
    print(f"  ✓ 全部区间交易")

    plot_and_print(results, [bh, band])


if __name__ == "__main__":
    main()
