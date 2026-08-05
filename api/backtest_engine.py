"""单只股票四策略回测引擎。

参考 strategy/backtest_baijiu_3in1.py 的三合一策略逻辑, 对单只股票分别运行:

  策略A (买入持有): 开盘即买入全部资金, 一直持有
  策略B (限价买入持有): 收盘价 ≤ 限价(买入价) 才买入全部资金, 之后一直持有
  策略C (区间交易): 收盘价 ≤ 买入价 买入全部; 收盘价 ≥ 卖出价 或 ≤ 止损价 卖出全部
  策略D (低价买入): 买入: N 日最低价 且 收盘价 ≤ 买入价
                    卖出: 收盘价 ≥ 卖出价 或 (持仓涨幅 > 阈值) 或 收盘价 ≤ 止损价

输出: 四条每日收益曲线 + 指标 (总收益率 / 年化 / 最大回撤 / 卡玛 / 夏普)。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

import numpy as np
import pandas as pd

DEFAULT_GAIN_THRESHOLD = 20.0  # 低价策略涨幅卖出阈值 (%)


# ---------------------------------------------------------------------------
# 三个子策略
# ---------------------------------------------------------------------------

def strat_buy_hold(df: pd.DataFrame, capital: float) -> list[dict]:
    """策略A: 买入并持有 — 首日买入全部资金, 一直持有。"""
    if df.empty:
        return []
    close0 = float(df["close"].iloc[0])
    shares = capital / close0 if close0 > 0 else 0.0
    daily = []
    for _, row in df.iterrows():
        daily.append({"date": row["trade_date"], "value": shares * float(row["close"])})
    return daily


def strat_limit_buy_hold(df: pd.DataFrame, capital: float, buy_price: float) -> list[dict]:
    """策略D: 限价买入持有 — 收盘价 ≤ 限价(buy_price) 时买入全部资金, 之后一直持有。

    与普通买入持有不同, 该策略在价格回落至限价以内之前不建仓;
    一旦买入即持有至期末。若整个区间价格始终高于限价则始终空仓。
    """
    shares = 0.0
    cash = capital
    daily = []
    for _, row in df.iterrows():
        close = float(row["close"])
        if shares == 0.0 and close <= buy_price and close > 0:
            shares = cash / close
            cash = 0.0
        daily.append({"date": row["trade_date"], "value": cash + shares * close})
    return daily


def strat_band_trade(df: pd.DataFrame, capital: float,
                     buy_price: float, sell_price: float, stop_loss: float) -> list[dict]:
    """策略B: 区间交易 — 收盘 ≤ 买入价 买入全部; ≥ 卖出价 或 ≤ 止损价 卖出全部。

    T+1 规则: 买入当天不检查卖出 (if/elif), 至少持有到下一交易日才能卖出;
    剔除无效交易: 卖出价≈买入价(收益≈0)时跳过本次卖出, 继续持有。
    """
    shares = 0.0
    cash = capital
    bought = False
    hold_price = 0.0  # 持仓成本价 (买入日收盘价)
    daily = []
    for _, row in df.iterrows():
        close = float(row["close"])
        if not bought and close <= buy_price:
            if buy_price > 0 and close > 0:
                shares = cash / close
                cash = 0.0
                bought = True
                hold_price = close
        elif bought and (close >= sell_price or close <= stop_loss):
            if abs(close - hold_price) <= 1e-9:
                # 买入价=卖出价 的无效交易 (收益≈0): 跳过, 继续持有
                daily.append({"date": row["trade_date"], "value": cash + shares * close})
                continue
            cash = shares * close
            shares = 0.0
            bought = False
        daily.append({"date": row["trade_date"], "value": cash + shares * close})
    return daily


def strat_low_price(df: pd.DataFrame, capital: float, lookback_days: int,
                    buy_price: float, sell_price: float, stop_loss: float,
                    gain_threshold: float = DEFAULT_GAIN_THRESHOLD) -> list[dict]:
    """策略C: 低价买入策略。

    买入: 当日收盘价为 N 日最低价 且 收盘价 ≤ 买入价
    卖出 (任一): 收盘价 ≥ 卖出价 | 持仓涨幅 > gain_threshold | 收盘价 ≤ 止损价
    """
    shares = 0.0
    cash = capital
    total_cost = 0.0
    window: deque[float] = deque(maxlen=lookback_days)
    daily = []

    for _, row in df.iterrows():
        date = row["trade_date"]
        close = float(row["close"])
        window.append(close)

        buy_signal = sell_signal = False
        sell_amount = 0.0

        if len(window) == lookback_days and close > 0:
            n_day_low = min(window)

            # --- 买入: N 日最低 + 价格 ≤ 买入价 且 有足够现金 ---
            if close == n_day_low and close <= buy_price and cash >= capital:
                buy_signal = True

            # --- 卖出 ---
            if shares > 0:
                gain_pct = ((shares * close - total_cost) / total_cost * 100
                            if total_cost > 0 else 0.0)
                cond_target = close >= sell_price
                cond_gain = gain_pct > gain_threshold
                cond_stop = close <= stop_loss
                if cond_target or cond_gain or cond_stop:
                    sell_amount = shares * close
                    sell_signal = True

        if buy_signal:
            shares = capital / close
            cash = 0.0
            total_cost = capital
        elif sell_signal:
            cash += sell_amount
            shares = 0.0
            total_cost = 0.0

        daily.append({"date": date, "value": cash + shares * close})

    return daily


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def _calc_metrics(daily: list[dict], capital: float) -> dict:
    """根据每日净值曲线计算: 总收益率/年化/最大回撤/卡玛/夏普。"""
    if not daily:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
                "calmar": 0.0, "sharpe": 0.0, "final_value": capital,
                "total_return_pct": 0.0}

    final = daily[-1]["value"]
    years = max((datetime.strptime(daily[-1]["date"], "%Y%m%d") -
                 datetime.strptime(daily[0]["date"], "%Y%m%d")).days / 365.25, 1e-9)

    ret_pct = (final / capital - 1) * 100 if capital > 0 else 0.0
    ann_pct = (pow(final / capital, 1 / years) - 1) * 100 if (capital > 0 and final > 0) else 0.0

    # 最大回撤 (%)
    peak = capital
    mdd = 0.0
    for dv in daily:
        v = dv["value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd

    # 卡玛比率 = 年化收益率 / 最大回撤
    calmar = ann_pct / mdd if mdd > 0 else 0.0

    # 夏普比率 = (年化收益率 - 无风险利率) / 年化波动率 (无风险利率=0)
    values = [dv["value"] for dv in daily]
    daily_returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values))
                     if values[i - 1] > 0]
    if daily_returns:
        ann_vol = float(np.std(daily_returns, ddof=1)) * np.sqrt(252)
        sharpe = (ann_pct / 100) / ann_vol if ann_vol > 0 else 0.0
    else:
        sharpe = 0.0

    return {"total_return": float(ret_pct), "annual_return": float(ann_pct),
            "max_drawdown": float(mdd), "calmar": float(calmar), "sharpe": float(sharpe),
            "final_value": float(final), "total_return_pct": float(ret_pct)}


# ---------------------------------------------------------------------------
# 汇总回测入口
# ---------------------------------------------------------------------------

STRATEGIES = ("买入持有", "限价买入持有", "区间交易", "低价买入")


def run_backtest(df: pd.DataFrame, capital: float,
                 buy_price: float, sell_price: float, stop_loss: float,
                 lookback_days: int = 20,
                 gain_threshold: float = DEFAULT_GAIN_THRESHOLD) -> list[dict]:
    """对单只股票运行四种策略, 返回各策略的每日净值曲线与指标。"""
    strategies = [
        {"name": "买入持有", "daily": strat_buy_hold(df, capital)},
        {"name": "限价买入持有", "daily": strat_limit_buy_hold(df, capital, buy_price)},
        {"name": "区间交易", "daily": strat_band_trade(df, capital, buy_price, sell_price, stop_loss)},
        {"name": "低价买入", "daily": strat_low_price(df, capital, lookback_days,
                                                       buy_price, sell_price, stop_loss, gain_threshold)},
    ]

    results = []
    for s in strategies:
        daily = s["daily"]
        metrics = _calc_metrics(daily, capital)
        # 收益率曲线 (%)
        returns_pct = [(dv["value"] / capital - 1) * 100 for dv in daily]
        results.append({
            "name": s["name"],
            "dates": [dv["date"] for dv in daily],
            "values": [round(dv["value"], 2) for dv in daily],
            "returns_pct": [round(r, 4) for r in returns_pct],
            "metrics": metrics,
        })
    return results
