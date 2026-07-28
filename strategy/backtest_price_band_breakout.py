#!/usr/bin/env python3
"""
价格区间 + N日突破策略回测（支持股票和ETF）。

策略规则:
  - 初始资金 30,000 元
  - 价格区间限制:
    五粮液(000858.SZ): 买入价 ≤ 75, 卖出价 ≥ 150
    中概ETF(513050.SH): 买入价 ≤ 1.1, 卖出价 ≥ 1.5
  - N 日价格突破:
    当日收盘价 == N 日最低价 → 买入 1 万元
    当日收盘价 == N 日最高价 → 卖出 1 万元
  - 买入限制: 同一周期内第 2/3 次买入价不得高于第 1 次（最多 3 次）
  - 卖出限制: 持仓总收益 > 30%, 且卖出后持仓 ≥ 2 万元
"""

import os
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import tushare as ts


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

INITIAL_CASH = 30_000.0      # 初始资金
TRADE_AMOUNT = 10_000.0      # 每次买卖金额
LOOKBACK_DAYS = 20           # N 日窗口
SELL_PROFIT_THRESHOLD = 30.0 # 卖出触发持仓收益率（百分比）
MIN_POSITION_VALUE = 20_000.0  # 卖出后最低持仓市值

# 513050 成立于 2017-01-18
START_DATE = "20170118"
END_DATE = "20260727"

# 价格区间限制（None 表示不限制）
PRICE_BANDS = {
    "513050.SH":  {"name": "中概互联ETF", "buy_max": 1.1,  "sell_min": 1.5,  "is_etf": True},
    "000858.SZ":  {"name": "五粮液",      "buy_max": 75.0, "sell_min": 150.0, "is_etf": False},
}


# ---------------------------------------------------------------------------
# Token 加载
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
                    key, value = line.split("=", 1)
                    os.environ[key] = value


def _init_pro() -> ts.pro_api:
    _load_env_token()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置 TUSHARE_TOKEN，请在 .env 文件中配置。")
    ts.set_token(token)
    return ts.pro_api(token)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    date: str
    action: str
    price: float
    amount: float
    shares: float
    cash_after: float
    shares_after: float
    n_day_low: float
    n_day_high: float


@dataclass
class BacktestResult:
    ts_code: str
    name: str
    initial_cash: float
    buy_max: Optional[float]
    sell_min: Optional[float]
    start_date: str
    end_date: str
    total_days: int
    lookback_days: int
    trade_count: int
    buy_count: int
    sell_count: int
    final_cash: float
    final_shares: float
    final_nav: float
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    buy_hold_return_pct: float
    trades: list[TradeRecord] = field(default_factory=list)
    daily_values: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 回测引擎
# ---------------------------------------------------------------------------

def fetch_daily_data(ts_code: str, start: str, end: str, is_etf: bool) -> pd.DataFrame:
    pro = _init_pro()
    if is_etf:
        df = pro.fund_daily(ts_code=ts_code, start_date=start, end_date=end)
    else:
        df = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
    if df.empty:
        raise ValueError(f"未获取到 {ts_code} 的日线数据。")
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df


def run_backtest(
    ts_code: str = "513050.SH",
    name: str = "中概互联ETF",
    start_date: str = START_DATE,
    end_date: str = END_DATE,
    initial_cash: float = INITIAL_CASH,
    trade_amount: float = TRADE_AMOUNT,
    lookback_days: int = LOOKBACK_DAYS,
    is_etf: bool = True,
    buy_max: Optional[float] = None,
    sell_min: Optional[float] = None,
    sell_profit_threshold: float = SELL_PROFIT_THRESHOLD,
    min_position_value: float = MIN_POSITION_VALUE,
) -> BacktestResult:
    """
    运行价格区间 + N 日突破策略回测。

    买入条件 (全部满足):
      1. 收盘价 == N 日最低价
      2. 现金 ≥ 交易金额
      3. (可选) 收盘价 ≤ buy_max
      4. 同一周期内: 第 2/3 次买入价 < 第 1 次买入价（最多 3 次）

    卖出条件 (全部满足):
      1. 收盘价 == N 日最高价
      2. 持仓市值 ≥ 交易金额
      3. (可选) 收盘价 ≥ sell_min
      4. 持仓总收益率 > sell_profit_threshold
      5. 卖出后持仓市值 ≥ min_position_value
    """
    df = fetch_daily_data(ts_code, start_date, end_date, is_etf=is_etf)

    cash = initial_cash
    shares = 0.0
    total_cost_basis = 0.0
    trades: list[TradeRecord] = []
    daily_values: list[dict] = []

    peak_value = initial_cash
    max_drawdown = 0.0

    first_buy_price_in_cycle = 0.0
    buy_count_in_cycle = 0

    window: deque[float] = deque(maxlen=lookback_days)

    for _, row in df.iterrows():
        date = row["trade_date"]
        close = float(row["close"])
        portfolio_value = cash + shares * close

        # 最大回撤
        if portfolio_value > peak_value:
            peak_value = portfolio_value
        drawdown = (peak_value - portfolio_value) / peak_value * 100
        if drawdown > max_drawdown:
            max_drawdown = drawdown

        window.append(close)

        buy_signal = False
        sell_signal = False
        n_day_low = 0.0
        n_day_high = 0.0

        if len(window) == lookback_days:
            n_day_low = min(window)
            n_day_high = max(window)

            # ---- 买入信号 ----
            price_ok = (buy_max is None) or (close <= buy_max)
            if close == n_day_low and cash >= trade_amount and price_ok:
                if buy_count_in_cycle >= 1:
                    price_ok = close < first_buy_price_in_cycle
                if price_ok and buy_count_in_cycle < 3:
                    buy_signal = True

            # ---- 卖出信号 ----
            price_ok = (sell_min is None) or (close >= sell_min)
            if close == n_day_high and shares * close >= trade_amount and price_ok:
                if total_cost_basis > 0 and shares > 0:
                    unrealized_return = (shares * close - total_cost_basis) / total_cost_basis * 100
                else:
                    unrealized_return = 0.0
                remaining_value = shares * close - trade_amount
                if unrealized_return > sell_profit_threshold and remaining_value >= min_position_value:
                    sell_signal = True

        # ---------- 执行交易 ----------
        if buy_signal:
            buy_shares = trade_amount / close
            shares += buy_shares
            cash -= trade_amount
            total_cost_basis += trade_amount
            if buy_count_in_cycle == 0:
                first_buy_price_in_cycle = close
            buy_count_in_cycle += 1
            trades.append(TradeRecord(
                date=date, action="买入", price=close,
                amount=trade_amount, shares=buy_shares,
                cash_after=cash, shares_after=shares,
                n_day_low=n_day_low, n_day_high=n_day_high,
            ))
        elif sell_signal:
            sell_shares = trade_amount / close
            total_cost_basis *= (1 - sell_shares / shares)
            shares -= sell_shares
            cash += trade_amount
            first_buy_price_in_cycle = 0.0
            buy_count_in_cycle = 0
            trades.append(TradeRecord(
                date=date, action="卖出", price=close,
                amount=trade_amount, shares=sell_shares,
                cash_after=cash, shares_after=shares,
                n_day_low=n_day_low, n_day_high=n_day_high,
            ))

        daily_values.append({
            "date": date, "close": close,
            "cash": cash, "shares": shares,
            "portfolio_value": cash + shares * close,
        })

    # --- 最终计算 ---
    last_close = float(df["close"].iloc[-1])
    first_close = float(df["close"].iloc[0])
    final_nav = cash + shares * last_close
    total_return = (final_nav / initial_cash - 1) * 100

    years = (datetime.strptime(end_date, "%Y%m%d") -
             datetime.strptime(start_date, "%Y%m%d")).days / 365.25
    annual_return = (pow(final_nav / initial_cash, 1 / years) - 1) * 100 if years > 0 else 0.0

    buy_hold_return = (last_close / first_close - 1) * 100

    buy_count = sum(1 for t in trades if t.action == "买入")
    sell_count = sum(1 for t in trades if t.action == "卖出")

    return BacktestResult(
        ts_code=ts_code, name=name,
        initial_cash=initial_cash,
        buy_max=buy_max, sell_min=sell_min,
        start_date=start_date, end_date=end_date,
        total_days=len(df), lookback_days=lookback_days,
        trade_count=len(trades),
        buy_count=buy_count, sell_count=sell_count,
        final_cash=cash, final_shares=shares, final_nav=final_nav,
        total_return_pct=total_return, annual_return_pct=annual_return,
        max_drawdown_pct=max_drawdown,
        buy_hold_return_pct=buy_hold_return,
        trades=trades, daily_values=daily_values,
    )


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def print_result(result: BacktestResult) -> None:
    sep = "=" * 56

    buy_limit = f"买入价≤{result.buy_max}" if result.buy_max else "无限制"
    sell_limit = f"卖出价≥{result.sell_min}" if result.sell_min else "无限制"

    print(f"\n{sep}")
    print(f"  {result.name}({result.ts_code}) 价格区间+突破策略回测报告")
    print(f"{sep}")
    print(f"  策略参数")
    print(f"  {'-' * 40}")
    print(f"    初始资金:     {result.initial_cash:>8,.0f} 元")
    print(f"    单笔买卖:     {TRADE_AMOUNT:>8,.0f} 元")
    print(f"    回溯窗口:     最近 {result.lookback_days} 个交易日")
    print(f"    价格区间:     买入 {buy_limit} / 卖出 {sell_limit}")
    print(f"    买入限制:     第2/3次买入价 < 第1次（最多3次）")
    print(f"    卖出限制:     持仓收益 > {SELL_PROFIT_THRESHOLD:.0f}% 且剩余 ≥ {MIN_POSITION_VALUE/1000:.0f}K")
    print(f"    回测区间:     {result.start_date} ~ {result.end_date}")
    print(f"    交易日数:     {result.total_days}")
    print()
    print(f"  交易统计")
    print(f"  {'-' * 40}")
    print(f"    总交易次数:   {result.trade_count} 次")
    print(f"    买入次数:     {result.buy_count} 次")
    print(f"    卖出次数:     {result.sell_count} 次")
    print()
    print(f"  收益分析")
    print(f"  {'-' * 40}")
    print(f"    最终总资产:   {result.final_nav:>8,.2f} 元")
    print(f"      ├─ 现金:    {result.final_cash:>8,.2f} 元")
    print(f"      └─ 持仓:    {result.final_shares:.2f} 份")
    print(f"    总收益率:     {result.total_return_pct:>+7.2f}%")
    print(f"    年化收益率:   {result.annual_return_pct:>+7.2f}%")
    print(f"    最大回撤:     {result.max_drawdown_pct:>7.2f}%")
    print()
    print(f"  对比基准")
    print(f"  {'-' * 40}")
    print(f"    同期买入持有: {result.buy_hold_return_pct:>+7.2f}%")
    print(f"    策略超额收益: {result.total_return_pct - result.buy_hold_return_pct:>+7.2f}%")
    print(f"{sep}")

    if result.trades:
        print(f"\n  所有交易记录:")
        print(f"  {'日期':<10} {'操作':<6} {'成交价':>8}")
        print(f"  {'-' * 28}")
        for t in result.trades:
            print(f"  {t.date:<10} {t.action:<6} {t.price:>8.3f}")

    # 年末快照
    print(f"\n  年末持仓快照:")
    print(f"  {'年份':<8} {'年末资产':>10} {'累计收益':>10}")
    print(f"  {'-' * 30}")
    yearly_snapshots = _calc_yearly_snapshots(result)
    for ys in yearly_snapshots[-5:]:
        print(f"  {ys['year']:<8} {ys['value']:>10,.0f} {ys['return']:>+9.2f}%")


def _calc_yearly_snapshots(result: BacktestResult) -> list[dict]:
    from collections import defaultdict
    yearly = defaultdict(list)
    for dv in result.daily_values:
        yearly[dv["date"][:4]].append(dv)
    snapshots = []
    for year in sorted(yearly.keys()):
        last = yearly[year][-1]
        val = last["portfolio_value"]
        ret = (val / result.initial_cash - 1) * 100
        snapshots.append({"year": year, "value": val, "return": ret})
    return snapshots


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="价格区间+N日突破策略回测")
    parser.add_argument("--code", default="513050.SH", help="证券代码")
    parser.add_argument("--name", default="中概互联ETF", help="证券名称")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS, help="N 日窗口")
    parser.add_argument("--type", choices=["etf", "stock"], default="etf")
    parser.add_argument("--buy-max", type=float, default=None, help="买入最高限价")
    parser.add_argument("--sell-min", type=float, default=None, help="卖出最低限价")
    parser.add_argument("--profit-threshold", type=float, default=SELL_PROFIT_THRESHOLD, help="卖出收益率阈值(%)")
    args = parser.parse_args()

    try:
        result = run_backtest(
            ts_code=args.code, name=args.name,
            lookback_days=args.lookback,
            is_etf=(args.type == "etf"),
            buy_max=args.buy_max,
            sell_min=args.sell_min,
            sell_profit_threshold=args.profit_threshold,
        )
        print_result(result)
    except (ValueError, RuntimeError) as e:
        print(f"[错误] {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
