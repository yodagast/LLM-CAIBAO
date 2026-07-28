#!/usr/bin/env python3
"""
中概ETF 513050 20日波动突破策略回测。

策略规则:
  - 初始资金 30,000 元
  - 每个交易日, 对比当日涨跌幅与过去 20 个交易日的极值:
    - 当日跌幅超过过去 20 天最大跌幅 → 买入 10,000 元
    - 当日涨幅超过过去 20 天最大涨幅 → 卖出 10,000 元
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

INITIAL_CASH = 30_000.0   # 初始资金
TRADE_AMOUNT = 10_000.0   # 每次买卖金额
LOOKBACK_DAYS = 90        # 回溯窗口

# 513050 成立于 2017-01-18
START_DATE = "20170118"
END_DATE = "20260727"


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
    pct_chg: float           # 当日涨跌幅
    ref_max_decline: float   # 20日窗口最大跌幅（当日买入参考值）
    ref_max_gain: float      # 20日窗口最大涨幅（当日卖出参考值）


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

def fetch_daily_data(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """获取ETF日线数据并按日期升序排列。"""
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
    lookback_days: int = LOOKBACK_DAYS,
) -> BacktestResult:
    """
    运行 20 日波动突破策略回测。

    每个交易日:
      1. 取过去 LOOKBACK_DAYS 个交易日（不含当日）的涨跌幅
      2. 找出其中的最大跌幅（最负值）和最大涨幅（最正值）
      3. 若当日跌幅超过该最大跌幅 → 买入
      4. 若当日涨幅超过该最大涨幅 → 卖出
    """
    df = fetch_daily_data(ts_code, start_date, end_date)

    cash = initial_cash
    shares = 0.0
    trades: list[TradeRecord] = []
    daily_values: list[dict] = []

    peak_value = initial_cash
    max_drawdown = 0.0

    # 滑动窗口：保存过去 lookback_days 个交易日的 pct_chg
    window: deque[float] = deque(maxlen=lookback_days)

    for idx, row in df.iterrows():
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

        # ---------- 信号判断 ----------
        # 窗口填满之前不产生信号（积累历史数据）
        buy_signal = False
        sell_signal = False
        ref_max_decline = 0.0
        ref_max_gain = 0.0

        if len(window) == lookback_days:
            ref_max_decline = min(window)  # 20日内最大跌幅（最负值）
            ref_max_gain = max(window)     # 20日内最大涨幅（最正值）

            # 当日跌幅超过历史最大跌幅 → 买入
            if pct_chg < ref_max_decline and cash >= trade_amount:
                buy_signal = True

            # 当日涨幅超过历史最大涨幅 → 卖出
            if pct_chg > ref_max_gain and shares * close >= trade_amount:
                sell_signal = True

        # ---------- 执行交易 ----------
        if buy_signal:
            buy_shares = trade_amount / close
            shares += buy_shares
            cash -= trade_amount
            trades.append(TradeRecord(
                date=date, action="买入", price=close,
                amount=trade_amount, shares=buy_shares,
                cash_after=cash, shares_after=shares,
                pct_chg=pct_chg,
                ref_max_decline=ref_max_decline,
                ref_max_gain=ref_max_gain,
            ))
        elif sell_signal:
            sell_shares = trade_amount / close
            shares -= sell_shares
            cash += trade_amount
            trades.append(TradeRecord(
                date=date, action="卖出", price=close,
                amount=trade_amount, shares=sell_shares,
                cash_after=cash, shares_after=shares,
                pct_chg=pct_chg,
                ref_max_decline=ref_max_decline,
                ref_max_gain=ref_max_gain,
            ))

        daily_values.append({
            "date": date,
            "close": close,
            "pct_chg": pct_chg,
            "cash": cash,
            "shares": shares,
            "portfolio_value": cash + shares * close,
            "ref_max_decline": ref_max_decline,
            "ref_max_gain": ref_max_gain,
        })

        # 将当日涨跌幅加入窗口（供后续交易日使用）
        window.append(pct_chg)

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
    print(f"  {result.etf_name}({result.etf_code}) 20日波动突破策略回测报告")
    print(f"{sep}")
    print(f"  策略参数")
    print(f"  {'-' * 40}")
    print(f"    初始资金:     {result.initial_cash:>8,.0f} 元")
    print(f"    单笔买卖金额: {TRADE_AMOUNT:>8,.0f} 元")
    print(f"    回溯窗口:     最近 {result.lookback_days} 个交易日")
    print(f"    买入条件:     当日跌幅 > 20日最大跌幅")
    print(f"    卖出条件:     当日涨幅 > 20日最大涨幅")
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
        print(f"  {'日期':<10} {'操作':<6} {'成交价':>7} {'当日涨跌':>8} {'触发阈值':>8}")
        print(f"  {'-' * 43}")
        for t in result.trades[-10:]:
            ref_val = t.ref_max_decline if t.action == "买入" else t.ref_max_gain
            print(f"  {t.date:<10} {t.action:<6} {t.price:>7.3f}  {t.pct_chg:>+7.2f}%  {ref_val:>+7.2f}%")

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
    parser = argparse.ArgumentParser(description="中概ETF 513050 20日波动突破策略回测")
    parser.add_argument("--etf", default=ETF_CODE, help=f"ETF代码（默认 {ETF_CODE}）")
    parser.add_argument("--cash", type=float, default=INITIAL_CASH, help="初始资金")
    parser.add_argument("--amount", type=float, default=TRADE_AMOUNT, help="单笔买卖金额")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS, help="回溯窗口（默认 20）")
    args = parser.parse_args()

    try:
        result = run_backtest(
            ts_code=args.etf,
            initial_cash=args.cash,
            trade_amount=args.amount,
            lookback_days=args.lookback,
        )
        print_result(result)
    except (ValueError, RuntimeError) as e:
        print(f"[错误] {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
