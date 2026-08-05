"""区间交易参数自动估算引擎。

需求: 输入股票代码与历史区间, 自动估算"区间交易"策略的 买入价/卖出价/止损价,
使回测收益尽可能高, 同时夏普比率尽量 ≥ 目标值 (默认 1.0)。

策略逻辑 (与 backtest_engine.strat_band_trade 一致):
  收盘价 ≤ 买入价          → 全仓买入
  收盘价 ≥ 卖出价 或 ≤ 止损价 → 清仓卖出

搜索方法:
  1. 网格搜索: 买入价取历史收盘价 P20~P80 分位 13 档;
     卖出价 = 买入价 × {1.05..1.6}; 止损价 = 买入价 × {0.5..0.95};
  2. 局部细化: 在最优组合附近再细搜一轮 (微调半径 ~6%);
  3. 选优 (objective):
       return (默认, 收益优先): 在 夏普≥min_sharpe 的组合中取收益最高;
       sharpe (夏普优先):       在 夏普≥min_sharpe 的组合中取夏普最高;
     若没有达标组合, 按 收益×(1+0.25×max(夏普,0)) 综合评分取折中, 并标记未达标。

性能: numpy 加速的净值模拟 + 向量化指标, 约 1100 组参数 × 2000+ 交易日,
      单只股票耗时 1~3 秒。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 卖出价 = 买入价 × SELL_RATIOS (均 > 1); 止损价 = 买入价 × STOP_RATIOS (均 < 1)
SELL_RATIOS = (1.05, 1.10, 1.15, 1.20, 1.25, 1.30, 1.40, 1.50, 1.60)
STOP_RATIOS = (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)

_BUY_GRID_N = 13          # 买入价候选档数
_BUY_PCT = (20.0, 80.0)   # 买入价分位区间
_REFINE_RADIUS = 0.06     # 局部细化半径


def _simulate_band(closes: np.ndarray, capital: float,
                   buy: float, sell: float, stop: float) -> np.ndarray:
    """区间交易净值模拟 (numpy 加速版), 返回每日净值数组。"""
    n = len(closes)
    value = np.empty(n)
    shares = 0.0
    cash = capital
    bought = False
    for i in range(n):
        c = closes[i]
        if not bought and c <= buy:
            if c > 0:
                shares = cash / c
                cash = 0.0
                bought = True
        if bought and (c >= sell or c <= stop):
            cash = shares * c
            shares = 0.0
            bought = False
        value[i] = cash + shares * c
    return value


def _fast_metrics(value: np.ndarray, capital: float,
                  periods_per_year: int = 252,
                  years: float | None = None) -> dict:
    """由净值数组快速计算指标 (向量化), 与 backtest_engine._calc_metrics 口径一致。

    years 为真实年数 (按日历天数/365.25); 未提供时回退为 交易日数/252。
    """
    n = len(value)
    if n == 0:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
                "calmar": 0.0, "sharpe": 0.0, "final_value": capital}
    final = float(value[-1])
    if years is None:
        years = n / periods_per_year
    years = max(float(years), 1e-9)
    ret = final / capital if capital > 0 else 0.0
    ret_pct = (ret - 1) * 100.0
    ann_pct = (pow(ret, 1 / years) - 1) * 100.0 if (ret > 0 and capital > 0) else 0.0

    peak = np.maximum.accumulate(value)
    dd = (peak - value) / np.where(peak > 0, peak, 1.0)
    mdd = float(np.max(dd)) * 100.0 if n else 0.0
    calmar = ann_pct / mdd if mdd > 0 else 0.0

    prev = value[:-1]
    rets = value[1:] / prev - 1.0
    rets = rets[prev > 0]
    if len(rets) > 1:
        ann_vol = float(np.std(rets, ddof=1)) * np.sqrt(periods_per_year)
        sharpe = (ann_pct / 100.0) / ann_vol if ann_vol > 0 else 0.0
    else:
        sharpe = 0.0

    return {"total_return": float(ret_pct), "annual_return": float(ann_pct),
            "max_drawdown": float(mdd), "calmar": float(calmar), "sharpe": float(sharpe),
            "final_value": float(final)}


def _candidate_grid(closes: np.ndarray) -> list[tuple[float, float, float]]:
    """网格候选: 买入价 P20~P80 分位 13 档 × 卖出价 9 档 × 止损价 8 档。

    卖出价=买入价×{1.05..1.6} > 买入价, 止损价=买入价×{0.5..0.95} < 买入价,
    round 到 2 位后仍严格满足 卖出价>买入价>止损价。
    """
    lo, hi = np.percentile(closes, list(_BUY_PCT))
    if hi <= lo:
        hi = lo + 1e-9
    buys = np.linspace(lo, hi, _BUY_GRID_N)
    cands: list[tuple[float, float, float]] = []
    for b in buys:
        for sratio in SELL_RATIOS:
            s = b * sratio
            for tratio in STOP_RATIOS:
                st = b * tratio
                rb, rs, rst = round(float(b), 2), round(float(s), 2), round(float(st), 2)
                if not (rs > rb > rst):  # 防御: round 后仍须满足卖出>买入>止损
                    continue
                cands.append((rb, rs, rst))
    return cands


def _refine_around(best: tuple[float, float, float],
                   radius: float = _REFINE_RADIUS) -> list[tuple[float, float, float]]:
    """在最优组合附近生成细网格候选。

    关键: 用 round 后的 2 位小数判断 卖出价>买入价>止损价, 避免 round 后
    出现卖出价≤买入价 的非法/无意义参数 (曾导致 buy==sell 却算出收益)。
    """
    b, s, st = best
    bgrid = np.linspace(b * (1 - radius), b * (1 + radius), 7)
    sgrid = np.linspace(s * (1 - radius * 0.8), s * (1 + radius), 5)
    stgrid = np.linspace(st * (1 - radius), st * (1 + radius * 0.8), 5)
    cands: list[tuple[float, float, float]] = []
    for bb in bgrid:
        for ss in sgrid:
            for sst in stgrid:
                rb, rs, rst = round(float(bb), 2), round(float(ss), 2), round(float(sst), 2)
                # round 后校验: 卖出价 > 买入价 > 止损价 (严格), 否则跳过
                if not (rs > rb > rst):
                    continue
                cands.append((rb, rs, rst))
    return cands


# 优化目标 → (排序字段, 方向); 方向 "max" 取最大, "min" 取最小
# "balanced" 为综合评分 (收益为主, 夏普加成, 回撤惩罚), 不在此表内
OBJECTIVES = {
    "return":   ("total_return",  "max"),
    "annual":   ("annual_return", "max"),
    "sharpe":   ("sharpe",        "max"),
    "drawdown": ("max_drawdown",  "min"),
    "calmar":   ("calmar",        "max"),
}

OBJECTIVE_LABELS = {
    "return":   "收益优先",
    "annual":   "年化收益优先",
    "sharpe":   "夏普优先",
    "drawdown": "回撤最小",
    "calmar":   "卡玛优先",
    "balanced": "综合平衡",
}


def _norm_objective(objective: str) -> str:
    """归一化目标名, 未知目标回退到收益优先。"""
    obj = (objective or "return").strip().lower()
    return obj if (obj in OBJECTIVES or obj == "balanced") else "return"


def _fallback_score(r: dict) -> float:
    """未达标时的综合评分: 收益主导, 夏普加成 25%。"""
    m = r["metrics"]
    return m["total_return"] * (1 + 0.25 * max(m["sharpe"], 0.0))


def _balanced_score(r: dict) -> float:
    """综合平衡评分: 收益为主, 夏普加成 50%, 回撤惩罚 (回撤越大得分越低)。"""
    m = r["metrics"]
    dd_penalty = 1 + m["max_drawdown"] / 100.0
    return m["total_return"] * (1 + 0.5 * max(m["sharpe"], 0.0)) / dd_penalty


def _pick_best(results: list[dict], min_sharpe: float, objective: str) -> dict:
    """在满足夏普≥min_sharpe 的组合中按目标选优; 无达标时用综合折中评分。"""
    objective = _norm_objective(objective)
    eligible = [r for r in results if r["metrics"]["sharpe"] >= min_sharpe]
    if not eligible:
        # 未达标: 综合折中, 避免选到纯空仓/极端组合
        return max(results, key=_fallback_score)
    if objective == "balanced":
        return max(eligible, key=_balanced_score)
    field, direction = OBJECTIVES[objective]
    if direction == "min":
        # 回撤最小; 同回撤时收益高者优先
        return min(eligible, key=lambda r: (r["metrics"][field], -r["metrics"]["total_return"]))
    # 主指标最大; 同值时收益高者优先 (tie-break)
    return max(eligible, key=lambda r: (r["metrics"][field], r["metrics"]["total_return"]))


def optimize_band(df: pd.DataFrame, capital: float = 100000.0,
                  min_sharpe: float = 1.0, objective: str = "return") -> dict:
    """搜索区间交易最优 (买入价/卖出价/止损价), 返回参数+曲线+指标+买入持有基准。"""
    objective = _norm_objective(objective)
    closes = df["close"].to_numpy(dtype=float)
    dates = df["trade_date"].astype(str).tolist()
    n = len(closes)
    if n < 30:
        raise ValueError(f"历史数据不足 ({n} 个交易日), 至少需要 30 个交易日。")

    # 真实年数 (日历天数/365.25), 与 backtest_engine._calc_metrics 口径一致
    from datetime import datetime
    _d0 = datetime.strptime(dates[0], "%Y%m%d")
    _d1 = datetime.strptime(dates[-1], "%Y%m%d")
    real_years = max((_d1 - _d0).days / 365.25, 1e-9)

    results: list[dict] = []

    def _eval(buy: float, sell: float, stop: float) -> None:
        # 防御: 区间交易要求 卖出价 > 买入价 > 止损价, 非法参数直接跳过
        if not (sell > buy > stop):
            return
        value = _simulate_band(closes, capital, buy, sell, stop)
        results.append({"buy_price": buy, "sell_price": sell, "stop_price": stop,
                        "value": value,
                        "metrics": _fast_metrics(value, capital, years=real_years)})

    # 1) 网格搜索
    for buy, sell, stop in _candidate_grid(closes):
        _eval(buy, sell, stop)

    # 2) 局部细化 (围绕当前最优)
    seed = _pick_best(results, min_sharpe, objective)
    for buy, sell, stop in _refine_around(
            (seed["buy_price"], seed["sell_price"], seed["stop_price"])):
        _eval(buy, sell, stop)

    # 3) 最终选优
    best = _pick_best(results, min_sharpe, objective)
    achieved = bool(best["metrics"]["sharpe"] >= min_sharpe)

    # 基准: 买入持有 (首日全仓)
    base_value = capital * closes / closes[0] if closes[0] > 0 else np.full(n, capital)
    base_metrics = _fast_metrics(base_value, capital, years=real_years)

    return {
        "params": {
            "buy_price": best["buy_price"],
            "sell_price": best["sell_price"],
            "stop_price": best["stop_price"],
            "total_return": best["metrics"]["total_return"],
            "sharpe": best["metrics"]["sharpe"],
            "achieved": achieved,
        },
        "search": {
            "tried": len(results),
            "min_sharpe": min_sharpe,
            "objective": objective,
            "objective_label": OBJECTIVE_LABELS[objective],
            "achieved": achieved,
        },
        "band": {
            "dates": dates,
            "returns_pct": [round((float(v) / capital - 1) * 100, 4) for v in best["value"]],
            "metrics": best["metrics"],
        },
        "baseline": {
            "dates": dates,
            "returns_pct": [round((float(v) / capital - 1) * 100, 4) for v in base_value],
            "metrics": base_metrics,
        },
        "range": {
            "start": dates[0],
            "end": dates[-1],
            "bars": n,
            "first_close": float(closes[0]),
            "last_close": float(closes[-1]),
        },
    }
