#!/usr/bin/env python3
"""
多策略回测对比 + 曲线绘制。

对比三组策略:
  1. 主力策略: N日突破+价格区间+收益阈值卖出
  2. 对照组1: 买入并持有 (Buy & Hold)
  3. 对照组2: 简单区间交易 (低于限价买入全部, 高于限价卖出1/2)

股票: 五粮液(000858.SZ), 招商银行(600036.SH)
各投入 30,000 元, 分别运行后汇总组合收益。
"""

import os
import sys
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import tushare as ts

# 检查绘图依赖
try:
    import matplotlib
    matplotlib.use("Agg")  # 非交互后端
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    from matplotlib.ticker import FuncFormatter
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False
    print("[警告] matplotlib 未安装, 将跳过绘图。")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

TRADE_AMOUNT = 10_000.0
SELL_GAIN_THRESHOLD = 20.0
MIN_HOLD_VALUE = 10_000.0
START_DATE = "20170101"
END_DATE = "20260728"
N_VALUES = [5, 20, 60, 120, 250]

STOCK_CONFIGS = {
    "000858.SZ": {
        "name": "五粮液",
        "initial_cash": 30_000.0,
        "buy_max_price": 75.0,
        "sell_price_threshold": 150.0,
        "is_etf": False,
    },
    "600036.SH": {
        "name": "招商银行",
        "initial_cash": 30_000.0,
        "buy_max_price": 30.0,
        "sell_price_threshold": 40.0,  # 用户要求 ≥40 可卖2万
        "is_etf": False,
    },
}

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
PLOT_PATH = os.path.join(OUTPUT_DIR, "backtest_comparison.png")
CSV_PATH = os.path.join(OUTPUT_DIR, "backtest_comparison.csv")


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
    """获取并缓存日线数据。"""
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
# 策略 1: N日突破 + 价格区间 + 收益卖出 (主力策略)
# ---------------------------------------------------------------------------

@dataclass
class StrategyRun:
    """单次策略运行结果。"""
    label: str
    portfolio_daily: list[dict]   # [{"date","portfolio_value"},...]
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    details: str = ""


def run_main_strategy(
    stock_configs: dict,
    lookback_days: int,
) -> StrategyRun:
    """主力策略: N日突破 + 价格区间 + 收益阈值卖出。"""
    all_portfolio: dict[str, list] = {}

    for ts_code, cfg in stock_configs.items():
        df = get_daily(ts_code, cfg["is_etf"])
        cash = cfg["initial_cash"]
        shares = 0.0
        total_cost = 0.0
        first_buy_price = 0.0
        buy_count_in_cycle = 0
        window: deque[float] = deque(maxlen=lookback_days)
        daily_vals: list[dict] = []

        for _, row in df.iterrows():
            date = row["trade_date"]
            close = float(row["close"])
            window.append(close)

            buy_signal = sell_signal = False
            sell_amount = 0.0

            if len(window) == lookback_days:
                n_day_low = min(window)

                # --- 买入 ---
                cond_price = (cfg["buy_max_price"] is not None) and (close <= cfg["buy_max_price"])
                cond_nday = (close == n_day_low)
                if (cond_price or cond_nday) and cash >= TRADE_AMOUNT:
                    price_ok = True
                    if buy_count_in_cycle >= 1:
                        price_ok = close < first_buy_price
                    if price_ok and buy_count_in_cycle < 3:
                        buy_signal = True

                # --- 卖出 ---
                if total_cost > 0 and shares > 0:
                    gain_pct = (shares * close - total_cost) / total_cost * 100
                else:
                    gain_pct = 0.0

                if gain_pct > SELL_GAIN_THRESHOLD and shares * close >= TRADE_AMOUNT:
                    max_sell = TRADE_AMOUNT
                    if cfg["sell_price_threshold"] is not None and close >= cfg["sell_price_threshold"]:
                        max_sell = TRADE_AMOUNT * 2
                    max_allowed = shares * close - MIN_HOLD_VALUE
                    if max_allowed >= TRADE_AMOUNT:
                        sell_amount = min(max_sell, max_allowed)
                        sell_signal = True

            if buy_signal:
                bshares = TRADE_AMOUNT / close
                shares += bshares
                cash -= TRADE_AMOUNT
                total_cost += TRADE_AMOUNT
                if buy_count_in_cycle == 0:
                    first_buy_price = close
                buy_count_in_cycle += 1
            elif sell_signal:
                sshares = sell_amount / close
                total_cost *= (1 - sshares / shares)
                shares -= sshares
                cash += sell_amount
                first_buy_price = 0.0
                buy_count_in_cycle = 0

            daily_vals.append({"date": date, "value": cash + shares * close})

        all_portfolio[ts_code] = daily_vals

    # 合并组合
    merged = _merge_portfolios(all_portfolio)
    ret, ann, mdd = _calc_metrics(merged, sum(c["initial_cash"] for c in stock_configs.values()))
    return StrategyRun(
        label=f"主力策略 N={lookback_days}",
        portfolio_daily=merged,
        total_return_pct=ret, annual_return_pct=ann, max_drawdown_pct=mdd,
    )


# ---------------------------------------------------------------------------
# 策略 2: 买入并持有 (BH)
# ---------------------------------------------------------------------------

def run_buy_hold(stock_configs: dict) -> StrategyRun:
    """买入并持有。"""
    all_portfolio = {}
    for ts_code, cfg in stock_configs.items():
        df = get_daily(ts_code, cfg["is_etf"])
        first_close = float(df["close"].iloc[0])
        last_close = float(df["close"].iloc[-1])
        shares_bought = cfg["initial_cash"] / first_close
        daily_vals = []
        for _, row in df.iterrows():
            close = float(row["close"])
            daily_vals.append({"date": row["trade_date"], "value": shares_bought * close})
        all_portfolio[ts_code] = daily_vals

    merged = _merge_portfolios(all_portfolio)
    ret, ann, mdd = _calc_metrics(merged, sum(c["initial_cash"] for c in stock_configs.values()))
    return StrategyRun(
        label="买入并持有",
        portfolio_daily=merged,
        total_return_pct=ret, annual_return_pct=ann, max_drawdown_pct=mdd,
    )


# ---------------------------------------------------------------------------
# 策略 3: 简单区间交易 (限价买入全部 / 超过限价卖出1/2)
# ---------------------------------------------------------------------------

def run_band_trade(stock_configs: dict) -> StrategyRun:
    """
    简单区间交易:
      - 价格 ≤ buy_max_price: 用全部可用现金买入
      - 价格 ≥ sell_price_threshold: 卖出 1/2 持仓
      初始半仓。
    """
    all_portfolio = {}
    for ts_code, cfg in stock_configs.items():
        df = get_daily(ts_code, cfg["is_etf"])
        cash = cfg["initial_cash"] * 0.5  # 初始半仓
        shares = (cfg["initial_cash"] * 0.5) / float(df["close"].iloc[0])
        daily_vals = []

        for _, row in df.iterrows():
            close = float(row["close"])

            # 买入: 价格 ≤ 限价 且 有现金
            if cfg["buy_max_price"] is not None and close <= cfg["buy_max_price"] and cash >= TRADE_AMOUNT:
                buy_shares = cash / close
                shares += buy_shares
                cash = 0.0

            # 卖出: 价格 ≥ 阈值 且 有持仓
            if cfg["sell_price_threshold"] is not None and close >= cfg["sell_price_threshold"] and shares > 0:
                sell_shares = shares * 0.5  # 卖一半
                cash += sell_shares * close
                shares -= sell_shares

            daily_vals.append({"date": row["trade_date"], "value": cash + shares * close})

        all_portfolio[ts_code] = daily_vals

    merged = _merge_portfolios(all_portfolio)
    ret, ann, mdd = _calc_metrics(merged, sum(c["initial_cash"] for c in stock_configs.values()))
    return StrategyRun(
        label="区间交易(买全部/卖1/2)",
        portfolio_daily=merged,
        total_return_pct=ret, annual_return_pct=ann, max_drawdown_pct=mdd,
    )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _merge_portfolios(all_portfolio: dict[str, list]) -> list[dict]:
    """合并多只股票的每日组合净值。"""
    date_map: dict[str, float] = defaultdict(float)
    for vals in all_portfolio.values():
        for dv in vals:
            date_map[dv["date"]] += dv["value"]
    merged = [{"date": d, "portfolio_value": v} for d, v in sorted(date_map.items())]
    return merged


def _calc_metrics(portfolio_daily: list[dict], initial_cash: float):
    """计算收益率、年化收益率、最大回撤。"""
    if not portfolio_daily:
        return 0.0, 0.0, 0.0
    final = portfolio_daily[-1]["portfolio_value"]
    total_ret = (final / initial_cash - 1) * 100

    years = (datetime.strptime(END_DATE, "%Y%m%d") -
             datetime.strptime(START_DATE, "%Y%m%d")).days / 365.25
    ann_ret = (pow(final / initial_cash, 1 / years) - 1) * 100 if years > 0 else 0.0

    peak = initial_cash
    max_dd = 0.0
    for dv in portfolio_daily:
        v = dv["portfolio_value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return total_ret, ann_ret, max_dd


# ---------------------------------------------------------------------------
# 绘图
# ---------------------------------------------------------------------------

def _format_pct(x, _):
    return f"{x:.0f}%"

def plot_results(all_runs: list[StrategyRun], bh_run: StrategyRun, band_run: StrategyRun) -> None:
    """绘制对比曲线图。"""
    if not HAVE_MPL:
        print("[跳过] matplotlib 不可用。")
        return

    # 中文字体 (macOS 可用 PingFang SC / Heiti SC)
    for font_name in ["PingFang SC", "Heiti SC", "Apple SD Gothic Neo",
                       "WenQuanYi Micro Hei", "Noto Sans CJK SC", "SimHei",
                       "DejaVu Sans"]:
        try:
            plt.rcParams["font.sans-serif"] = [font_name]
            plt.rcParams["axes.unicode_minus"] = False
            # 验证字体可用
            fig_test, ax_test = plt.subplots(figsize=(1, 1))
            ax_test.set_title("测试中文")
            plt.close(fig_test)
            break
        except Exception:
            continue

    fig, ax = plt.subplots(figsize=(14, 7))

    # 颜色方案
    colors_main = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    colors_ref = ["#aaaaaa", "#8c564b"]

    # 绘制主力策略 (不同 N)
    for i, run in enumerate(all_runs):
        dates = [dv["date"] for dv in run.portfolio_daily]
        values = [dv["portfolio_value"] for dv in run.portfolio_daily]
        initial = sum(c["initial_cash"] for c in STOCK_CONFIGS.values())
        pcts = [(v / initial - 1) * 100 for v in values]
        ax.plot(dates, pcts, label=run.label, color=colors_main[i % len(colors_main)],
                linewidth=1.5, alpha=0.9)

    # 买入持有
    dates_bh = [dv["date"] for dv in bh_run.portfolio_daily]
    values_bh = [dv["portfolio_value"] for dv in bh_run.portfolio_daily]
    initial = sum(c["initial_cash"] for c in STOCK_CONFIGS.values())
    pcts_bh = [(v / initial - 1) * 100 for v in values_bh]
    ax.plot(dates_bh, pcts_bh, label=bh_run.label, color=colors_ref[0],
            linewidth=2.5, linestyle="--", alpha=0.8)

    # 区间交易
    dates_band = [dv["date"] for dv in band_run.portfolio_daily]
    values_band = [dv["portfolio_value"] for dv in band_run.portfolio_daily]
    pcts_band = [(v / initial - 1) * 100 for v in values_band]
    ax.plot(dates_band, pcts_band, label=band_run.label, color=colors_ref[1],
            linewidth=2.5, linestyle=":", alpha=0.8)

    # 装饰
    ax.set_title("五粮液 + 招商银行 组合策略回测对比 (2017~2026)", fontsize=14, fontweight="bold")
    ax.set_ylabel("累计收益率 (%)", fontsize=12)
    ax.set_xlabel("日期", fontsize=12)
    ax.yaxis.set_major_formatter(FuncFormatter(_format_pct))
    ax.axhline(y=0, color="gray", linewidth=0.5, linestyle="-")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.savefig(PLOT_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n[图表] 已保存: {PLOT_PATH}")


# ---------------------------------------------------------------------------
# 结果表格
# ---------------------------------------------------------------------------

def print_table(all_runs: list[StrategyRun], bh_run: StrategyRun, band_run: StrategyRun) -> None:
    """打印汇总表格。"""
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  多策略回测对比 (初始资金合计: 60,000 元)")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"{sep}")
    print(f"  {'策略':<22} {'总收益率':>10} {'年化':>8} {'最大回撤':>8}")
    print(f"  {'-' * 50}")

    for run in all_runs:
        print(f"  {run.label:<22} {run.total_return_pct:>+9.2f}% {run.annual_return_pct:>+7.2f}% {run.max_drawdown_pct:>7.2f}%")

    print(f"  {'-' * 50}")
    print(f"  {bh_run.label:<22} {bh_run.total_return_pct:>+9.2f}% {bh_run.annual_return_pct:>+7.2f}% {bh_run.max_drawdown_pct:>7.2f}%")
    print(f"  {band_run.label:<22} {band_run.total_return_pct:>+9.2f}% {band_run.annual_return_pct:>+7.2f}% {band_run.max_drawdown_pct:>7.2f}%")
    print(f"{sep}")

    # 存 CSV
    rows = []
    for run in all_runs + [bh_run, band_run]:
        rows.append({
            "策略": run.label,
            "总收益率%": round(run.total_return_pct, 2),
            "年化收益率%": round(run.annual_return_pct, 2),
            "最大回撤%": round(run.max_drawdown_pct, 2),
        })
    pd.DataFrame(rows).to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    print(f"[CSV] 已保存: {CSV_PATH}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    print("正在获取数据...")
    # 预先加载所有数据
    for ts_code, cfg in STOCK_CONFIGS.items():
        get_daily(ts_code, cfg["is_etf"])
        print(f"  ✓ {cfg['name']}({ts_code}) 数据已加载")

    print("\n正在运行策略回测...")

    # 运行对照组 (只跑一次)
    bh_run = run_buy_hold(STOCK_CONFIGS)
    print(f"  ✓ 买入并持有")

    band_run = run_band_trade(STOCK_CONFIGS)
    print(f"  ✓ 区间交易")

    # 运行主力策略 (多个 N)
    main_runs: list[StrategyRun] = []
    for n in N_VALUES:
        run = run_main_strategy(STOCK_CONFIGS, lookback_days=n)
        main_runs.append(run)
        print(f"  ✓ 主力策略 N={n}")

    # 输出表格
    print_table(main_runs, bh_run, band_run)

    # 绘图
    plot_results(main_runs, bh_run, band_run)


if __name__ == "__main__":
    main()
