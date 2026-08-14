"""组合回测服务: 从股票列表做 买入持有 + 区间交易(自动估算最优买卖/止损价) 回测, 等权组合聚合。

链路 (由 api/main.py 调用):
  1. 逐只 resolve_code + get_daily(前复权) 拉区间日线
  2. band_service.optimize_band 自动估算最优 买入价/卖出价/止损价, 并给出 区间交易 与 买入持有
     两条净值曲线与指标 (买入持有即 optimize_band 的 baseline)
  3. 等权日再平衡聚合: 每只分配 initial_capital/N, 组合日收益 = 各只日收益均值 → 累计成组合净值

用途: 前端「买入持有回测」tab 的「组合回测」子面板, 可从策略 Hub 导入股票列表。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd

from . import band_service, data_service


async def _load_daily(sym: str, start: str, end: str):
    """解析代码并拉区间日线 (前复权)。返回 (info, df)。"""
    info = await data_service.resolve_code(sym)
    kind = info["kind"]
    adj = "qfq" if kind == "stock" else ""
    df = await data_service.get_daily(sym, kind=kind, start_date=start, end_date=end, adj=adj)
    return info, df


def _metrics_series(returns_pct: list, dates: list[str]) -> pd.Series:
    """累计收益% → pd.Series(index=str 日期)。"""
    return pd.Series(returns_pct, index=dates)


def _aggregate(band_series: dict, bh_series: dict, capital: float, n: int) -> dict:
    """等权日再平衡聚合: 组合日收益 = 各只日收益均值, 累计成组合净值。

    band_series / bh_series: {sym: pd.Series(index=str YYYYMMDD, value=累计收益%)}。
    """
    all_dates = sorted(set().union(
        *[s.index for s in band_series.values()],
        *[s.index for s in bh_series.values()],
    ))
    if not all_dates:
        return {}
    idx = pd.DatetimeIndex(pd.to_datetime(all_dates))
    per = capital / n if n > 0 else capital

    # 累计收益% → 净值
    def _net(df_dict):
        out = pd.DataFrame(index=idx)
        for sym, s in df_dict.items():
            # band_series index 为 str YYYYMMDD, 需先转 datetime 才能 reindex 到 idx
            s2 = s.copy()
            s2.index = pd.to_datetime(s2.index)
            out[sym] = per * (1 + s2.reindex(idx).ffill().fillna(0) / 100.0)
        return out

    band_net = _net(band_series)
    bh_net = _net(bh_series)

    band_daily = band_net.pct_change().fillna(0.0).mean(axis=1)
    bh_daily = bh_net.pct_change().fillna(0.0).mean(axis=1)
    band_eq = (1 + band_daily).cumprod() * capital
    bh_eq = (1 + bh_daily).cumprod() * capital

    def _m(eq):
        ret = eq / capital
        n_days = len(eq)
        years = max(((idx[-1] - idx[0]).days / 365.25), 1e-9)
        total = (float(eq.iloc[-1]) / capital - 1) * 100
        ann = (pow(float(eq.iloc[-1]) / capital, 1 / years) - 1) * 100 if eq.iloc[-1] > 0 else 0.0
        peak = eq.cummax()
        mdd = float(((eq - peak) / peak * 100).min())
        daily = eq.pct_change().dropna()
        std = daily.std(ddof=1) * (252 ** 0.5) if len(daily) > 1 else 0.0
        sharpe = (ann / 100) / std if std > 0 else 0.0
        calmar = ann / mdd if mdd > 0 else 0.0
        return {"total_return": total, "annual_return": ann, "max_drawdown": mdd,
                "calmar": calmar, "sharpe": sharpe, "final_value": float(eq.iloc[-1]), "years": years}

    return {
        "dates": [d.strftime("%Y%m%d") for d in idx],
        "band": [round(float(v), 4) for v in (band_eq / capital - 1) * 100],
        "buyhold": [round(float(v), 4) for v in (bh_eq / capital - 1) * 100],
        "band_metrics": _m(band_eq),
        "buyhold_metrics": _m(bh_eq),
    }


async def backtest_portfolio(symbols: list[str], params: dict) -> dict:
    """组合回测: 逐股自动估算区间交易最优参数 + 买入持有, 等权聚合。

    params 关键字段: start_date / end_date / initial_capital / min_sharpe /
    objective(return|annual|sharpe|drawdown|calmar|balanced) / max_trades
    """
    capital = float(params.get("initial_capital") or 100000.0)
    start = params.get("start_date") or "20170101"
    end = params.get("end_date") or ""
    min_sharpe = float(params.get("min_sharpe") or 0.0)
    objective = params.get("objective") or "balanced"
    max_trades = int(params.get("max_trades") or 100)
    symbols = [s for s in (symbols or []) if s and s.strip()]
    n = len(symbols)
    per = capital / n if n else capital

    stocks: list[dict] = []
    band_series: dict[str, pd.Series] = {}
    bh_series: dict[str, pd.Series] = {}

    for sym in symbols:
        try:
            info, df = await _load_daily(sym, start, end)
            if df is None or len(df) < 30:
                stocks.append({"ts_code": sym, "name": info.get("name", sym), "ok": False,
                               "reason": "历史数据不足"})
                continue
            r = await asyncio.to_thread(
                band_service.optimize_band, df, capital=per,
                min_sharpe=min_sharpe, objective=objective, max_trades=max_trades)
            band_series[sym] = _metrics_series(r["band"]["returns_pct"], r["band"]["dates"])
            bh_series[sym] = _metrics_series(r["baseline"]["returns_pct"], r["baseline"]["dates"])
            stocks.append({
                "ts_code": sym,
                "name": info.get("name", sym),
                "kind": info.get("kind", "stock"),
                "ok": True,
                "range": r["range"],
                "params": r["params"],
                "band": {k: round(float(v), 4) for k, v in r["band"]["metrics"].items()},
                "buyhold": {k: round(float(v), 4) for k, v in r["baseline"]["metrics"].items()},
                "trades_count": len(r["trades"]),
                "trades": r["trades"],
            })
        except Exception as e:
            stocks.append({"ts_code": sym, "name": sym, "ok": False, "reason": str(e)[:120]})

    portfolio = _aggregate(band_series, bh_series, capital, len(band_series)) if band_series else {}
    return {
        "stocks": stocks,
        "portfolio": portfolio,
        "params": {
            "symbols": symbols,
            "start_date": start,
            "end_date": end,
            "initial_capital": capital,
            "min_sharpe": min_sharpe,
            "objective": objective,
            "max_trades": max_trades,
        },
    }
