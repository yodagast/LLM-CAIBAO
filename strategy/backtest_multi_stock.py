#!/usr/bin/env python3
"""
多股票组合策略回测。

策略规则 (每只股票独立运行):
  初始资金: 每只股票 30,000 元

  买入条件 (满足任一):
    A. 收盘价 ≤ buy_max_price（指定限价）
    B. 收盘价为最近 N 天的最低价
    每次最多买入 10,000 元

  卖出条件 (全部满足):
    A. 持仓收益率 > 20%
    B. 可卖出金额:
       - 默认最多卖出 10,000 元
       - 若收盘价 ≥ sell_price_threshold, 最多可卖出 20,000 元
    C. 卖出后持仓市值 ≥ min_hold_value (10,000 元)
"""

import os
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import tushare as ts


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 20            # N 日窗口
TRADE_AMOUNT = 10_000.0       # 每次基准买卖金额
SELL_GAIN_THRESHOLD = 10.0    # 卖出触发收益率 (%)
MIN_HOLD_VALUE = 10_000.0     # 卖出后最低持仓市值
START_DATE = "20170101"
END_DATE = "20260728"

# 股票配置: {ts_code: {name, initial_cash, buy_max_price, sell_price_threshold, is_etf}}
STOCK_CONFIGS = {
    "000858.SZ": {
        "name": "五粮液",
        "initial_cash": 30_000.0,
        "buy_max_price": 75.0,      # 买入限价
        "sell_price_threshold": 150.0,  # 可卖出 2 万的价格阈值
        "is_etf": False,
    },
    "600036.SH": {
        "name": "招商银行",
        "initial_cash": 30_000.0,
        "buy_max_price": 30.0,      # 买入限价
        "sell_price_threshold": 45.0,   # 可卖出 2 万的价格阈值
        "is_etf": False,
    },
    "513050.SH": {
        "name": "中概互联ETF",
        "initial_cash": 30_000.0,
        "buy_max_price": 1.0,      # 买入限价
        "sell_price_threshold": 1.5,   # 可卖出 2 万的价格阈值
        "is_etf": True,
    },
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
        raise RuntimeError("未设置 TUSHARE_TOKEN。")
    ts.set_token(token)
    return ts.pro_api(token)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    date: str
    ts_code: str
    name: str
    action: str
    price: float
    amount: float
    shares: float
    cash_after: float
    shares_after: float
    reason: str = ""


@dataclass
class StockResult:
    """单只股票的回测结果。"""
    ts_code: str
    name: str
    initial_cash: float
    buy_max_price: Optional[float]
    sell_price_threshold: Optional[float]
    total_days: int
    trade_count: int
    buy_count: int
    sell_count: int
    final_cash: float
    final_shares: float
    final_nav: float        # 期末总资产 (现金+持仓市值)
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    buy_hold_return_pct: float
    trades: list[TradeRecord] = field(default_factory=list)
    daily_values: list[dict] = field(default_factory=list)


@dataclass
class PortfolioResult:
    """组合回测结果。"""
    total_initial_cash: float
    total_final_nav: float
    total_return_pct: float
    total_annual_return_pct: float
    total_max_drawdown_pct: float
    stock_results: list[StockResult] = field(default_factory=list)
    portfolio_daily: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 单股票回测引擎
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


def run_single_stock(
    ts_code: str,
    name: str,
    start_date: str,
    end_date: str,
    initial_cash: float,
    lookback_days: int,
    is_etf: bool,
    buy_max_price: Optional[float] = None,
    sell_price_threshold: Optional[float] = None,
    trade_amount: float = TRADE_AMOUNT,
    sell_gain_threshold: float = SELL_GAIN_THRESHOLD,
    min_hold_value: float = MIN_HOLD_VALUE,
) -> StockResult:
    """
    单只股票回测。

    买入条件 (任一满足):
      1. 收盘价 ≤ buy_max_price
      2. 收盘价为 N 日最低价
      且现金 ≥ trade_amount

    卖出条件:
      1. 持仓收益率 > sell_gain_threshold
      2. max_sell = trade_amount (默认 1 万)
      3. 若收盘价 ≥ sell_price_threshold, max_sell = trade_amount * 2 (2 万)
      4. 卖出后持仓市值 ≥ min_hold_value
    """
    df = fetch_daily_data(ts_code, start_date, end_date, is_etf=is_etf)

    cash = initial_cash
    shares = 0.0
    total_cost = 0.0
    trades: list[TradeRecord] = []
    daily_values: list[dict] = []

    peak_value = initial_cash
    max_drawdown = 0.0

    # 买入周期追踪
    first_buy_price = 0.0
    buy_count_in_cycle = 0

    # N 日滑动窗口
    window: deque[float] = deque(maxlen=lookback_days)

    for _, row in df.iterrows():
        date = row["trade_date"]
        close = float(row["close"])
        position_value = shares * close
        portfolio_value = cash + position_value

        # 最大回撤
        if portfolio_value > peak_value:
            peak_value = portfolio_value
        drawdown = (peak_value - portfolio_value) / peak_value * 100
        if drawdown > max_drawdown:
            max_drawdown = drawdown

        window.append(close)

        buy_signal = False
        sell_signal = False
        sell_amount = 0.0
        buy_reason = ""
        sell_reason = ""

        if len(window) == lookback_days:
            n_day_low = min(window)
            n_day_high = max(window)

            # ========== 买入判断 ==========
            cond_price = (buy_max_price is not None) and (close <= buy_max_price)
            cond_nday = (close == n_day_low)

            if (cond_price or cond_nday) and cash >= trade_amount:
                # 买入周期限制: 第 2/3 次买入价 < 第 1 次
                price_ok = True
                if buy_count_in_cycle >= 1:
                    price_ok = close < first_buy_price
                if price_ok and buy_count_in_cycle < 3:
                    buy_signal = True
                    if cond_price and cond_nday:
                        buy_reason = f"限价({buy_max_price})+N日低"
                    elif cond_price:
                        buy_reason = f"限价≤{buy_max_price}"
                    else:
                        buy_reason = f"{lookback_days}日最低"

            # ========== 卖出判断 ==========
            if total_cost > 0 and shares > 0:
                gain_pct = (shares * close - total_cost) / total_cost * 100
            else:
                gain_pct = 0.0

            if gain_pct > sell_gain_threshold and position_value >= trade_amount:
                # 计算可卖出金额
                max_sell = trade_amount  # 默认 1 万
                sell_reason = f"涨幅{gain_pct:.1f}%>20%"
                if sell_price_threshold is not None and close >= sell_price_threshold:
                    max_sell = trade_amount * 2  # 可卖 2 万
                    sell_reason += f"且价格≥{sell_price_threshold}"

                # 卖出后持仓市值不低于 min_hold_value
                max_sell_allowed = position_value - min_hold_value
                if max_sell_allowed >= trade_amount:
                    sell_amount = min(max_sell, max_sell_allowed)
                    sell_signal = True

        # ---------- 执行 ----------
        if buy_signal:
            buy_shares = trade_amount / close
            shares += buy_shares
            cash -= trade_amount
            total_cost += trade_amount
            if buy_count_in_cycle == 0:
                first_buy_price = close
            buy_count_in_cycle += 1
            trades.append(TradeRecord(
                date, ts_code, name, "买入", close,
                trade_amount, buy_shares, cash, shares,
                reason=buy_reason,
            ))

        elif sell_signal:
            sell_shares = sell_amount / close
            total_cost *= (1 - sell_shares / shares) if shares > 0 else 0
            shares -= sell_shares
            cash += sell_amount
            # 重置买入周期
            first_buy_price = 0.0
            buy_count_in_cycle = 0
            trades.append(TradeRecord(
                date, ts_code, name, "卖出", close,
                sell_amount, sell_shares, cash, shares,
                reason=sell_reason,
            ))

        daily_values.append({
            "date": date, "close": close, "cash": cash,
            "shares": shares, "position_value": shares * close,
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

    return StockResult(
        ts_code=ts_code, name=name,
        initial_cash=initial_cash,
        buy_max_price=buy_max_price,
        sell_price_threshold=sell_price_threshold,
        total_days=len(df),
        trade_count=len(trades),
        buy_count=buy_count, sell_count=sell_count,
        final_cash=cash, final_shares=shares, final_nav=final_nav,
        total_return_pct=total_return, annual_return_pct=annual_return,
        max_drawdown_pct=max_drawdown,
        buy_hold_return_pct=buy_hold_return,
        trades=trades, daily_values=daily_values,
    )


# ---------------------------------------------------------------------------
# 组合回测
# ---------------------------------------------------------------------------

def run_portfolio(
    stock_configs: dict = None,
    start_date: str = START_DATE,
    end_date: str = END_DATE,
    lookback_days: int = LOOKBACK_DAYS,
) -> PortfolioResult:
    """运行组合回测（多只股票独立运行后汇总）。"""
    if stock_configs is None:
        stock_configs = STOCK_CONFIGS

    stock_results = []
    all_dates = set()

    for ts_code, cfg in stock_configs.items():
        result = run_single_stock(
            ts_code=ts_code,
            name=cfg["name"],
            start_date=start_date,
            end_date=end_date,
            initial_cash=cfg["initial_cash"],
            lookback_days=lookback_days,
            is_etf=cfg.get("is_etf", False),
            buy_max_price=cfg.get("buy_max_price"),
            sell_price_threshold=cfg.get("sell_price_threshold"),
        )
        stock_results.append(result)
        for dv in result.daily_values:
            all_dates.add(dv["date"])

    # 合并每日组合净值
    total_initial = sum(r.initial_cash for r in stock_results)
    total_final = sum(r.final_nav for r in stock_results)

    portfolio_daily = []
    for date in sorted(all_dates):
        total_value = sum(
            next((dv["portfolio_value"] for dv in r.daily_values if dv["date"] == date), 0)
            for r in stock_results
        )
        portfolio_daily.append({"date": date, "portfolio_value": total_value})

    # 组合最大回撤
    peak = total_initial
    max_dd = 0.0
    for pdv in portfolio_daily:
        v = pdv["portfolio_value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    total_return = (total_final / total_initial - 1) * 100
    years = (datetime.strptime(end_date, "%Y%m%d") -
             datetime.strptime(start_date, "%Y%m%d")).days / 365.25
    annual_return = (pow(total_final / total_initial, 1 / years) - 1) * 100 if years > 0 else 0.0

    return PortfolioResult(
        total_initial_cash=total_initial,
        total_final_nav=total_final,
        total_return_pct=total_return,
        total_annual_return_pct=annual_return,
        total_max_drawdown_pct=max_dd,
        stock_results=stock_results,
        portfolio_daily=portfolio_daily,
    )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def print_stock_result(r: StockResult) -> None:
    """打印单只股票回测结果。"""
    sep = "=" * 52
    buy_limit = f"≤{r.buy_max_price}" if r.buy_max_price else "不限"
    sell_limit = f"≥{r.sell_price_threshold}可卖2万" if r.sell_price_threshold else "仅卖1万"

    print(f"\n  {r.name}({r.ts_code})")
    print(f"  {'-' * 40}")
    print(f"    初始资金:     {r.initial_cash:>8,.0f} 元")
    print(f"    买入限价:     {buy_limit} 或 N日最低")
    print(f"    卖出条件:     收益>20% ({sell_limit}), 底舱≥1万")
    print(f"    最终总资产:   {r.final_nav:>8,.2f} 元")
    print(f"      ├─ 现金:    {r.final_cash:>8,.2f} 元")
    print(f"      └─ 持仓:    {r.final_shares:.2f} 份")
    print(f"    总收益率:     {r.total_return_pct:>+7.2f}%")
    print(f"    年化收益率:   {r.annual_return_pct:>+7.2f}%")
    print(f"    最大回撤:     {r.max_drawdown_pct:>7.2f}%")
    print(f"    交易次数:     {r.trade_count} 次 (买入{r.buy_count}/卖出{r.sell_count})")
    print(f"    同期买入持有: {r.buy_hold_return_pct:>+7.2f}%")

    if r.trades:
        print(f"\n    交易记录:")
        print(f"    {'日期':<10} {'操作':<6} {'成交价':>8} {'金额':>8} {'原因':<24}")
        print(f"    {'-' * 58}")
        for t in r.trades:
            print(f"    {t.date:<10} {t.action:<6} {t.price:>8.3f} {t.amount:>8,.0f} {t.reason:<24}")


def print_portfolio_result(pr: PortfolioResult) -> None:
    """打印组合回测结果。"""
    sep = "=" * 52

    print(f"\n{sep}")
    print(f"  组合回测报告")
    print(f"{sep}")
    print(f"  总初始资金: {pr.total_initial_cash:>8,.0f} 元")
    print(f"  总最终资产: {pr.total_final_nav:>8,.2f} 元")
    print(f"  组合总收益率: {pr.total_return_pct:>+7.2f}%")
    print(f"  组合年化收益率: {pr.total_annual_return_pct:>+7.2f}%")
    print(f"  组合最大回撤: {pr.total_max_drawdown_pct:>7.2f}%")

    for r in pr.stock_results:
        print_stock_result(r)

    print(f"\n{sep}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="多股票组合策略回测")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS, help="N 日窗口")
    parser.add_argument("--gain", type=float, default=SELL_GAIN_THRESHOLD, help="卖出收益率阈值(%)")
    args = parser.parse_args()

    try:
        pr = run_portfolio(
            lookback_days=args.lookback,
        )
        print_portfolio_result(pr)
    except (ValueError, RuntimeError) as e:
        print(f"[错误] {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
