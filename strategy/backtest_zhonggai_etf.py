#!/usr/bin/env python3
"""
中概ETF 513050 网格交易策略回测。

策略规则:
  - 初始资金 30,000 元
  - 每日跌幅超过 3% 时，买入 10,000 元 513050
  - 每日涨幅超过 3% 时，卖出 10,000 元 513050
  - 回测周期: 最近 10 年
"""

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import tushare as ts


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

ETF_CODE ="513050.SH" #"513050.SH"
ETF_NAME = "中概互联ETF"

INITIAL_CASH = 30_000.0   # 初始资金
TRADE_AMOUNT = 10_000.0   # 每次买卖金额
THRESHOLD_BUY = -4.0      # 买入阈值（跌幅超过此值）
THRESHOLD_SELL = 4.0      # 卖出阈值（涨幅超过此值）

# 513050 成立于 2017-01-18，最近约 9.5 年
START_DATE = "20170118"
END_DATE = "20260727"


# ---------------------------------------------------------------------------
# Token 加载
# ---------------------------------------------------------------------------

def _load_env_token(env_path: str = "../.env") -> None:
    """从 .env 文件加载 TUSHARE_TOKEN。"""
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
    """单笔交易记录。"""
    date: str
    action: str         # "买入" or "卖出"
    price: float        # 成交价格
    amount: float       # 成交金额
    shares: float       # 成交份额
    cash_after: float   # 交易后现金余额
    shares_after: float # 交易后持仓份额


@dataclass
class BacktestResult:
    """回测结果。"""
    etf_code: str
    etf_name: str
    initial_cash: float
    start_date: str
    end_date: str
    total_days: int
    trade_count: int
    buy_count: int
    sell_count: int
    final_cash: float
    final_shares: float
    final_nav: float        # 最终总资产 = cash + shares * last_close
    total_return_pct: float  # 总收益率
    annual_return_pct: float # 年化收益率
    max_drawdown_pct: float  # 最大回撤
    buy_hold_return_pct: float  # 同期买入持有收益率
    trades: list[TradeRecord] = field(default_factory=list)
    daily_values: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 回测引擎
# ---------------------------------------------------------------------------

def fetch_daily_data(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """获取ETF日线数据（使用 fund_daily 接口）并按日期升序排列。"""
    pro = _init_pro()
    df = pro.fund_daily(ts_code=ts_code, start_date=start, end_date=end)
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
    threshold_buy: float = THRESHOLD_BUY,
    threshold_sell: float = THRESHOLD_SELL,
) -> BacktestResult:
    """
    运行网格交易策略回测。

    每个交易日:
      - 若跌幅超过 threshold_buy (如 -3%) 且现金充足 → 买入 trade_amount 元
      - 若涨幅超过 threshold_sell (如 3%) 且持仓市值足够 → 卖出 trade_amount 元
    """
    df = fetch_daily_data(ts_code, start_date, end_date)

    cash = initial_cash
    shares = 0.0
    trades: list[TradeRecord] = []
    daily_values: list[dict] = []

    peak_value = initial_cash
    max_drawdown = 0.0

    for _, row in df.iterrows():
        date = row["trade_date"]
        close = float(row["close"])
        pct_chg = float(row["pct_chg"])
        portfolio_value = cash + shares * close

        # 跟踪最大回撤
        if portfolio_value > peak_value:
            peak_value = portfolio_value
        drawdown = (peak_value - portfolio_value) / peak_value * 100
        if drawdown > max_drawdown:
            max_drawdown = drawdown

        # --- 买入信号 ---
        if pct_chg < threshold_buy and cash >= trade_amount:
            buy_shares = trade_amount / close
            shares += buy_shares
            cash -= trade_amount
            trades.append(TradeRecord(
                date=date, action="买入", price=close,
                amount=trade_amount, shares=buy_shares,
                cash_after=cash, shares_after=shares,
            ))

        # --- 卖出信号 ---
        elif pct_chg > threshold_sell and shares * close >= trade_amount:
            sell_shares = trade_amount / close
            shares -= sell_shares
            cash += trade_amount
            trades.append(TradeRecord(
                date=date, action="卖出", price=close,
                amount=trade_amount, shares=sell_shares,
                cash_after=cash, shares_after=shares,
            ))

        daily_values.append({
            "date": date,
            "close": close,
            "pct_chg": pct_chg,
            "cash": cash,
            "shares": shares,
            "portfolio_value": cash + shares * close,
        })

    # --- 计算最终结果 ---
    last_close = float(df["close"].iloc[-1])
    first_close = float(df["close"].iloc[0])
    final_nav = cash + shares * last_close
    total_return = (final_nav / initial_cash - 1) * 100

    # 年化收益率
    years = (datetime.strptime(end_date, "%Y%m%d") -
             datetime.strptime(start_date, "%Y%m%d")).days / 365.25
    annual_return = (pow(final_nav / initial_cash, 1 / years) - 1) * 100 if years > 0 else 0.0

    # 同期买入持有收益率
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
    """打印回测结果。"""
    sep = "=" * 56

    print(f"\n{sep}")
    print(f"  {result.etf_name}({result.etf_code}) 网格策略回测报告")
    print(f"{sep}")
    print(f"  策略参数")
    print(f"  {'-' * 40}")
    print(f"    初始资金:     {result.initial_cash:>8,.0f} 元")
    print(f"    单笔买卖金额: {TRADE_AMOUNT:>8,.0f} 元")
    print(f"    买入阈值:     当日跌幅 > {abs(THRESHOLD_BUY):.0f}%")
    print(f"    卖出阈值:     当日涨幅 > {THRESHOLD_SELL:.0f}%")
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

    # 打印最近10笔交易
    if result.trades:
        print(f"\n  最近10笔交易记录:")
        print(f"  {'日期':<12} {'操作':<6} {'成交价':>8} {'金额':>10} {'份额':>10} {'剩余现金':>10}")
        print(f"  {'-' * 58}")
        for t in result.trades[-10:]:
            print(f"  {t.date:<12} {t.action:<6} {t.price:>8.3f} {t.amount:>10,.0f} {t.shares:>10.2f} {t.cash_after:>10,.0f}")

    # 年末快照
    print(f"\n  年末持仓快照:")
    print(f"  {'年份':<8} {'年末资产':>10} {'年化收益':>10}")
    print(f"  {'-' * 30}")
    yearly_snapshots = _calc_yearly_snapshots(result)
    for ys in yearly_snapshots[-5:]:  # 最近5年
        print(f"  {ys['year']:<8} {ys['value']:>10,.0f} {ys['return']:>+9.2f}%")


def _calc_yearly_snapshots(result: BacktestResult) -> list[dict]:
    """计算各年末快照。"""
    from collections import defaultdict
    yearly = defaultdict(list)
    for dv in result.daily_values:
        year = dv["date"][:4]
        yearly[year].append(dv)

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
    parser = argparse.ArgumentParser(description="中概ETF 513050 网格策略回测")
    parser.add_argument("--etf", default=ETF_CODE, help=f"ETF代码（默认 {ETF_CODE}）")
    parser.add_argument("--cash", type=float, default=INITIAL_CASH, help="初始资金")
    parser.add_argument("--amount", type=float, default=TRADE_AMOUNT, help="单笔买卖金额")
    parser.add_argument("--buy-threshold", type=float, default=THRESHOLD_BUY, help="买入阈值（跌幅）")
    parser.add_argument("--sell-threshold", type=float, default=THRESHOLD_SELL, help="卖出阈值（涨幅）")
    args = parser.parse_args()

    try:
        result = run_backtest(
            ts_code=args.etf,
            initial_cash=args.cash,
            trade_amount=args.amount,
            threshold_buy=args.buy_threshold,
            threshold_sell=args.sell_threshold,
        )
        print_result(result)
    except (ValueError, RuntimeError) as e:
        print(f"[错误] {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
