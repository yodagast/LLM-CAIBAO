#!/usr/bin/env python3
"""
中概ETF 513050 N日价格突破策略回测。

策略规则:
  - 初始资金 30,000 元
  - 每个交易日，将当日收盘价与过去 N 个交易日（含当日）比较:
    - 当日价格为 N 日最低价 → 买入 10,000 元
    - 当日价格为 N 日最高价 → 卖出 10,000 元
  - 回测周期: 自基金成立日至今
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

ETF_CODE = "513050.SH"
ETF_NAME = "中概互联ETF"

INITIAL_CASH = 30_000.0      # 初始资金
TRADE_AMOUNT = 10_000.0      # 每次买卖金额
LOOKBACK_DAYS = 20           # N 日窗口
SELL_PROFIT_THRESHOLD = 5.0  # 卖出触发总收益阈值（百分比）
MIN_POSITION_VALUE = 20_000.0  # 卖出后最低持仓市值

# 513050 成立于 2017-01-18
START_DATE = "20170118"
END_DATE = "20260727"

# 五粮液成立于更早，用同一回测区间方便对比
WULIANGYE_CODE = "000858.SZ"
WULIANGYE_NAME = "五粮液"


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
    n_day_low: float    # N 日最低价
    n_day_high: float   # N 日最高价


@dataclass
class BacktestResult:
    etf_code: str
    etf_name: str
    initial_cash: float
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

def fetch_daily_data(ts_code: str, start: str, end: str, is_etf: bool = True) -> pd.DataFrame:
    """获取日线数据（ETF 用 fund_daily，股票用 daily）并按日期升序排列。"""
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
    ts_code: str = ETF_CODE,
    etf_name: str = ETF_NAME,
    start_date: str = START_DATE,
    end_date: str = END_DATE,
    initial_cash: float = INITIAL_CASH,
    trade_amount: float = TRADE_AMOUNT,
    lookback_days: int = LOOKBACK_DAYS,
    is_etf: bool = True,
    sell_profit_threshold: float = SELL_PROFIT_THRESHOLD,
    min_position_value: float = MIN_POSITION_VALUE,
) -> BacktestResult:
    """
    运行 N 日价格突破策略回测。

    买入规则:
      1. 取过去 N 个交易日（含当日）的收盘价
      2. 当日收盘价 == N 日最低价且现金充足 → 买入 10,000 元
      3. 在一个买入周期内（卖出前），第 2/3 次买入价不得高于第 1 次买入价

    卖出规则:
      1. 取过去 N 个交易日（含当日）的收盘价
      2. 当日收盘价 == N 日最高价且持仓市值足够 → 检查以下条件:
         a. 持仓总收益超过 sell_profit_threshold%
         b. 卖出后持仓市值不低于 min_position_value 元
      3. 条件均满足则卖出 10,000 元
    """
    df = fetch_daily_data(ts_code, start_date, end_date, is_etf=is_etf)

    cash = initial_cash
    shares = 0.0
    total_cost_basis = 0.0        # 持仓部分的成本总额
    trades: list[TradeRecord] = []
    daily_values: list[dict] = []

    peak_value = initial_cash
    max_drawdown = 0.0

    # 买入周期追踪：卖出后重置
    first_buy_price_in_cycle = 0.0
    buy_count_in_cycle = 0

    # 滑动窗口：保存过去 lookback_days 个交易日的 close
    window: deque[float] = deque(maxlen=lookback_days)

    for idx, row in df.iterrows():
        date = row["trade_date"]
        close = float(row["close"])
        portfolio_value = cash + shares * close

        # 跟踪最大回撤
        if portfolio_value > peak_value:
            peak_value = portfolio_value
        drawdown = (peak_value - portfolio_value) / peak_value * 100
        if drawdown > max_drawdown:
            max_drawdown = drawdown

        # 将当日收盘价加入窗口
        window.append(close)

        buy_signal = False
        sell_signal = False
        n_day_low = 0.0
        n_day_high = 0.0

        # 窗口填满后才产生信号
        if len(window) == lookback_days:
            n_day_low = min(window)
            n_day_high = max(window)

            # ---- 买入信号 ----
            # 条件1: 当日价格为 N 日最低
            # 条件2: 现金充足
            # 条件3: 若为买入周期内第 2/3 次，价格不得高于第 1 次买入价
            if close == n_day_low and cash >= trade_amount:
                price_ok = True
                if buy_count_in_cycle >= 1:
                    # 第2次及以上买入，价格必须低于首次买入价
                    price_ok = close < first_buy_price_in_cycle
                # 限制最多买入 3 次（首次 + 2 次补仓）
                if price_ok and buy_count_in_cycle < 3:
                    buy_signal = True

            # ---- 卖出信号 ----
            # 条件1: 当日价格为 N 日最高
            # 条件2: 持仓市值 >= 卖出金额
            # 条件3: 持仓总收益率 > 阈值
            # 条件4: 卖出后持仓市值 >= 最低要求
            if close == n_day_high and shares * close >= trade_amount:
                # 计算持仓总收益率
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
            # 追踪买入周期
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
            # 按比例扣减成本 basis
            total_cost_basis *= (1 - sell_shares / shares)
            shares -= sell_shares
            cash += trade_amount
            # 重置买入周期（卖出后重新计数）
            first_buy_price_in_cycle = 0.0
            buy_count_in_cycle = 0
            trades.append(TradeRecord(
                date=date, action="卖出", price=close,
                amount=trade_amount, shares=sell_shares,
                cash_after=cash, shares_after=shares,
                n_day_low=n_day_low, n_day_high=n_day_high,
            ))

        daily_values.append({
            "date": date,
            "close": close,
            "cash": cash,
            "shares": shares,
            "portfolio_value": cash + shares * close,
            "n_day_low": n_day_low,
            "n_day_high": n_day_high,
        })

    # --- 计算最终结果 ---
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
        etf_code=ts_code,
        etf_name=etf_name,
        initial_cash=initial_cash,
        start_date=start_date,
        end_date=end_date,
        total_days=len(df),
        lookback_days=lookback_days,
        trade_count=len(trades),
        buy_count=buy_count,
        sell_count=sell_count,
        final_cash=cash,
        final_shares=shares,
        final_nav=final_nav,
        total_return_pct=total_return,
        annual_return_pct=annual_return,
        max_drawdown_pct=max_drawdown,
        buy_hold_return_pct=buy_hold_return,
        trades=trades,
        daily_values=daily_values,
    )


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def print_result(result: BacktestResult) -> None:
    sep = "=" * 56

    print(f"\n{sep}")
    print(f"  {result.etf_name}({result.etf_code}) N日价格突破策略回测报告")
    print(f"{sep}")
    print(f"  策略参数")
    print(f"  {'-' * 40}")
    print(f"    初始资金:     {result.initial_cash:>8,.0f} 元")
    print(f"    单笔买卖金额: {TRADE_AMOUNT:>8,.0f} 元")
    print(f"    回溯窗口:     最近 {result.lookback_days} 个交易日")
    print(f"    买入条件:     当日收盘价 == {result.lookback_days}日最低价")
    print(f"    买入限制:     第2/3次买入价不得高于第1次（最多3次）")
    print(f"    卖出条件:     当日收盘价 == {result.lookback_days}日最高价")
    print(f"    卖出限制:     持仓收益 > {SELL_PROFIT_THRESHOLD:.0f}% 且剩余持仓 ≥ {MIN_POSITION_VALUE/1000:.0f}K")
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

    # 最近10笔交易
    if result.trades:
        print(f"\n  最近10笔交易记录:")
        print(f"  {'日期':<10} {'操作':<6} {'成交价':>7} {'N日最低':>8} {'N日最高':>8}")
        print(f"  {'-' * 43}")
        for t in result.trades[-10:]:
            print(f"  {t.date:<10} {t.action:<6} {t.price:>7.3f}  {t.n_day_low:>7.3f}  {t.n_day_high:>7.3f}")

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
    parser = argparse.ArgumentParser(description="N日价格突破策略回测（支持股票和ETF）")
    parser.add_argument("--code", default=ETF_CODE, help=f"证券代码（默认 {ETF_CODE}）")
    parser.add_argument("--name", default=ETF_NAME, help="证券名称")
    parser.add_argument("--cash", type=float, default=INITIAL_CASH, help="初始资金")
    parser.add_argument("--amount", type=float, default=TRADE_AMOUNT, help="单笔买卖金额")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS, help="N 日窗口（默认 20）")
    parser.add_argument("--type", choices=["etf", "stock"], default="etf",
                        help="证券类型：etf（默认）或 stock")
    parser.add_argument("--profit-threshold", type=float, default=SELL_PROFIT_THRESHOLD,
                        help="卖出触发持仓收益率（默认 5%）")
    parser.add_argument("--min-position", type=float, default=MIN_POSITION_VALUE,
                        help="卖出后最低持仓市值（默认 20000）")
    args = parser.parse_args()

    try:
        result = run_backtest(
            ts_code=args.code,
            etf_name=args.name,
            initial_cash=args.cash,
            trade_amount=args.amount,
            lookback_days=args.lookback,
            is_etf=(args.type == "etf"),
            sell_profit_threshold=args.profit_threshold,
            min_position_value=args.min_position,
        )
        print_result(result)
    except (ValueError, RuntimeError) as e:
        print(f"[错误] {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
