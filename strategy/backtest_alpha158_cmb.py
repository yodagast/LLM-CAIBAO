"""基于 qlib Alpha158 + LightGBM 对招商银行(600036.SH)最近十年的低频回测。

流程 (参考 ashare-lowfreq-research 的 qlib 研究链路: 因子 -> 训练 -> 分数 -> 回测):
  1. qlib.init(provider_uri=storage/qlib_data/cn_data)  数据来自 scripts/export_qlib_data.py
  2. Alpha158 因子 (qlib.contrib.data.handler.Alpha158, 158 个量价因子)
  3. Walk-forward 年度滚动重训 LightGBM (expanding 窗口, 避免未来函数)
  4. 长/空 单标的策略: 预测次日收益>阈值 持仓, 否则空仓 (决策于当日收盘, 次日生效)
  5. 扣除 A 股交易成本 (佣金+印花税+过户费), 对比买入持有

用法:
  .venv/bin/python strategy/backtest_alpha158_cmb.py
  .venv/bin/python strategy/backtest_alpha158_cmb.py --threshold 0.0005 --retrain-years 1
  .venv/bin/python strategy/backtest_alpha158_cmb.py --test-start 20160801 --test-end 20260812

输出: reports/backtest_alpha158_600036_*.csv / .png
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
QLIB_DIR = ROOT / "storage" / "qlib_data" / "cn_data"
REPORT_DIR = ROOT / "reports"

SYMBOL = "600036.SH"

# --- 交易成本 (A 股) ---
COMMISSION = 0.00025   # 佣金 万2.5 (双边)
STAMP_DUTY = 0.0005    # 印花税 0.05% (仅卖出)
TRANSFER_FEE = 0.00001 # 过户费 0.001% (双边, 沪市)
BUY_COST = COMMISSION + TRANSFER_FEE
SELL_COST = COMMISSION + STAMP_DUTY + TRANSFER_FEE

# --- 默认区间 ---
DATA_START = "2012-01-01"   # Alpha158 因子计算起点 (含 60 日滚动 warmup)
TRAIN_START = "2013-01-01"  # 训练样本起点
TEST_START = "2016-08-01"   # 回测起点 (最近十年)
TEST_END = "2026-08-12"     # 回测终点 (数据最新交易日)


def build_alpha158_handler(start_time, end_time):
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset.handler import DataHandlerLP

    return Alpha158(
        instruments="all",
        start_time=start_time,
        end_time=end_time,
        freq="day",
        infer_processors=[],
        learn_processors=[],
        process_type=DataHandlerLP.PTYPE_A,
    )


def load_panel(handler):
    """获取 Alpha158 特征 + 标签, 返回 (df[instrument, date, feature..., LABEL0], feat_cols)。"""
    df = handler.fetch().reset_index()  # instrument/date 全部转列 (支持多股票池)
    df = df.rename(columns={"datetime": "date"})
    feat_cols = [c for c in df.columns if c not in ("instrument", "date", "LABEL0")]
    return df, feat_cols


def walk_forward_predict(df, feat_cols, chunks, train_start, gap_days=30):
    """年度滚动重训 LightGBM, 返回测试期预测 Series (index=date)。"""
    import lightgbm as lgb

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    pred_parts = []

    for ts, te in chunks:
        train_end = ts - pd.Timedelta(days=gap_days)
        train = df[(df["date"] >= train_start) & (df["date"] <= train_end)]
        test = df[(df["date"] >= ts) & (df["date"] < te)]

        train = train.dropna(subset=["LABEL0"])
        if len(train) < 200:
            print(f"  [{ts.date()}~{te.date()}] 训练样本不足({len(train)}), 跳过")
            continue

        Xtr, ytr = train[feat_cols], train["LABEL0"].astype(float)
        model = lgb.LGBMRegressor(
            n_estimators=400,
            learning_rate=0.05,
            num_leaves=31,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_samples=20,
            random_state=42,
            verbose=-1,
        )
        model.fit(Xtr, ytr)
        pred = model.predict(test[feat_cols])
        pred_df = pd.DataFrame({"date": test["date"].values, "pred": pred})
        pred_parts.append(pred_df)
        print(f"  [{ts.date()}~{te.date()}] train={len(train)} test={len(test)}")

    if not pred_parts:
        raise SystemExit("无可回测的预测结果")
    all_pred = pd.concat(pred_parts).drop_duplicates("date").set_index("date")["pred"]
    return all_pred.sort_index()


def calc_metrics(net_ret: pd.Series, dates: pd.Series, capital=100_000.0) -> dict:
    """按仓库 backtest_engine 口径: 总收益/年化/最大回撤/卡玛/夏普。"""
    net_ret = net_ret.fillna(0.0)
    equity = (1 + net_ret).cumprod() * capital
    final = float(equity.iloc[-1])
    years = max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 1e-9)

    total_ret = (final / capital - 1) * 100
    ann_ret = (pow(final / capital, 1 / years) - 1) * 100 if final > 0 else 0.0

    # 最大回撤
    peak = equity.cummax()
    mdd = float(((equity - peak) / peak * 100).min())

    # 夏普 (无风险利率=0)
    std = net_ret.std(ddof=1)
    ann_vol = std * np.sqrt(252)
    sharpe = (ann_ret / 100) / ann_vol if ann_vol > 0 else 0.0
    calmar = ann_ret / mdd if mdd > 0 else 0.0
    return {
        "total_return": total_ret,
        "annual_return": ann_ret,
        "max_drawdown": mdd,
        "calmar": calmar,
        "sharpe": sharpe,
        "final_value": final,
        "years": years,
    }


def run_backtest(close: pd.Series, pred: pd.Series,
                 enter_thr: float = 0.0, exit_thr: float | None = None,
                 min_holding: int = 1) -> dict:
    """长/空策略回测: 决策于当日收盘(用当日预测), 次日生效。

    - enter_thr: 预测>enter_thr 买入 (建仓)
    - exit_thr : 预测<exit_thr 卖出 (空仓); 默认=enter_thr (无滞回)
    - min_holding: 建仓后至少持有 N 天再允许卖出 (减少无效换手)
    """
    if exit_thr is None:
        exit_thr = enter_thr
    close = close.astype(float)
    ret = close.pct_change()

    # 逐日持仓状态机 (决策于当日, 次日生效)
    pred_s = pred.reindex(close.index)
    holding = 0
    hold_days = 0
    pos_arr = np.zeros(len(close))
    for i in range(len(close)):
        if holding == 0 and pred_s.iloc[i] > enter_thr:
            holding = 1
            hold_days = 0
        elif holding == 1:
            hold_days += 1
            if hold_days >= min_holding and pred_s.iloc[i] < exit_thr:
                holding = 0
        pos_arr[i] = holding
    pos = pd.Series(pos_arr, index=close.index).shift(1).fillna(0.0)  # 次日生效

    buy_turn = pos.diff().clip(lower=0).fillna(0.0)    # 0->1 买入
    sell_turn = (-pos.diff()).clip(lower=0).fillna(0.0)  # 1->0 卖出
    cost = buy_turn * BUY_COST + sell_turn * SELL_COST

    strat_ret = pos * ret
    net_ret = strat_ret - cost

    dates = close.index.to_series()
    m = calc_metrics(net_ret, dates)
    m.update({
        "exposure": float(pos.mean()),
        "n_trades": int((buy_turn > 0).sum()),
        "win_rate": float((strat_ret[pos > 0] > 0).mean()) if (pos > 0).any() else 0.0,
        "avg_holding": float(pos.mean() * len(pos) / max((buy_turn > 0).sum(), 1)),
        "total_cost": float(cost.sum() * 100),  # % of capital
        "enter_thr": enter_thr,
        "exit_thr": exit_thr,
        "min_holding": min_holding,
    })
    result = {
        "pos": pos, "strat_ret": strat_ret, "cost": cost, "net_ret": net_ret,
        "equity": (1 + net_ret).cumprod(), "metrics": m,
    }
    return result


def fmt_metrics(m: dict) -> str:
    extra = ""
    if "n_trades" in m:
        extra = f" | 换手 {m['n_trades']} 次 | 仓位 {m['exposure']*100:.1f}%"
    return (
        f"总收益 {m['total_return']:+.2f}% | 年化 {m['annual_return']:+.2f}% | "
        f"最大回撤 {m['max_drawdown']:.2f}% | 夏普 {m['sharpe']:.2f} | 卡玛 {m['calmar']:.2f} | "
        f"期末资产 {m['final_value']:,.0f}{extra}"
    )


def plot_result(close, result, bh_metrics, path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for fp in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"):
        if Path(fp).exists():
            font_manager.fontManager.addfont(fp)
            plt.rcParams["font.family"] = "PingFang SC"
            break
    plt.rcParams["axes.unicode_minus"] = False

    eq = result["equity"]
    bh = close / close.iloc[0]
    dd = (eq / eq.cummax() - 1) * 100

    fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1, 1]})
    axes[0].plot(eq.index, eq.values, label=f"Alpha158 策略 ({result['metrics']['total_return']:+.1f}%)",
                 color="#e65050", lw=1.5)
    axes[0].plot(bh.index, bh.values, label=f"买入持有 ({bh_metrics['total_return']:+.1f}%)",
                 color="#8a8f98", lw=1.2, ls="--")
    axes[0].set_title(title)
    axes[0].set_ylabel("净值")
    axes[0].legend(loc="upper left", fontsize=9)
    axes[0].grid(alpha=0.3)

    axes[1].fill_between(dd.index, dd.values, 0, color="#e65050", alpha=0.4)
    axes[1].set_ylabel("回撤(%)")
    axes[1].grid(alpha=0.3)

    axes[2].plot(result["pos"].index, result["pos"].values, color="#3b82f6", lw=0.8)
    axes[2].set_ylabel("持仓")
    axes[2].set_ylim(-0.1, 1.1)
    axes[2].grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="招商银行 Alpha158 十年回测")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--qlib-dir", default=str(QLIB_DIR))
    parser.add_argument("--data-start", default=DATA_START)
    parser.add_argument("--train-start", default=TRAIN_START)
    parser.add_argument("--test-start", default=TEST_START)
    parser.add_argument("--test-end", default=TEST_END)
    parser.add_argument("--enter-threshold", type=float, default=0.0,
                        help="买入阈值: 预测>此值建仓 (默认0)")
    parser.add_argument("--exit-threshold", type=float, default=None,
                        help="卖出阈值: 预测<此值空仓 (默认=买入阈值, 无滞回)")
    parser.add_argument("--min-holding", type=int, default=1,
                        help="建仓后最短持有天数 (默认1)")
    parser.add_argument("--sweep", action="store_true",
                        help="参数扫描: 对比多组 (买入/卖出阈值, 最短持有)")
    parser.add_argument("--capital", type=float, default=100_000.0)
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. qlib 初始化
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=args.qlib_dir, region="cn")
    print(f"qlib provider: {args.qlib_dir}")

    # 2. Alpha158 因子
    handler = build_alpha158_handler(args.data_start, args.test_end)
    df, feat_cols = load_panel(handler)
    df = df[df["instrument"].astype(str) == args.symbol] if "instrument" in df.columns else df
    print(f"Alpha158 因子: {len(feat_cols)} 个, 样本 {len(df)} 行 "
          f"({pd.to_datetime(df['date']).min().date()} ~ {pd.to_datetime(df['date']).max().date()})")

    # 3. Walk-forward 分段
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)
    chunks = []
    cur = test_start
    while cur < test_end:
        nxt = min(cur + pd.DateOffset(years=1), test_end)
        chunks.append((cur, nxt))
        cur = nxt
    print(f"回测区间: {test_start.date()} ~ {test_end.date()}  "
          f"分段: {[(str(a.date()), str(b.date())) for a, b in chunks]}")

    print("Walk-forward 训练:")
    pred = walk_forward_predict(df, feat_cols, chunks, pd.Timestamp(args.train_start))

    # 4. 回测 (需要收盘价; D.features 返回 MultiIndex, 需去掉 instrument 层)
    close_series = D.features([args.symbol], ["$close"], start_time=args.test_start,
                              end_time=args.test_end, freq="day")["$close"]
    close = close_series.droplevel("instrument").rename("close")
    close.index = pd.to_datetime(close.index)
    pred = pred.reindex(close.index)
    close = close.reindex(pred.index)
    idx = close.index.intersection(pred.index)
    close = close.loc[idx]
    pred = pred.loc[idx]

    bh_metrics = calc_metrics(close.pct_change(), close.index.to_series(), capital=args.capital)

    # 参数扫描模式: walk-forward 只跑一次, 回测轻量
    configs = []
    if args.sweep:
        configs = [
            (0.0, 0.0, 1), (0.0, 0.0, 5), (0.0005, 0.0, 5), (0.0005, 0.0, 10),
            (0.001, 0.0, 10), (0.001, 0.0, 20), (0.001, -0.0005, 10),
            (0.002, 0.0, 20), (0.002, -0.001, 20),
        ]
    else:
        configs = [(args.enter_threshold, args.exit_threshold, args.min_holding)]

    results = {}
    for enter, exit_, mh in configs:
        results[(enter, exit_, mh)] = run_backtest(
            close, pred, enter_thr=enter,
            exit_thr=(enter if exit_ is None else exit_), min_holding=mh)

    print("\n" + "=" * 100)
    print(f"招商银行({args.symbol}) 最近十年 Alpha158 回测 "
          f"({test_start.date()} ~ {test_end.date()})")
    print("=" * 100)

    if args.sweep:
        print("\n参数扫描 (买入阈值 / 卖出阈值 / 最短持有):")
        rows = []
        for (enter, exit_, mh), r in results.items():
            m = r["metrics"]
            rows.append({
                "买>": enter, "卖<": exit_, "持N天": mh,
                "总收益%": round(m["total_return"], 1),
                "年化%": round(m["annual_return"], 2),
                "回撤%": round(m["max_drawdown"], 1),
                "夏普": round(m["sharpe"], 2),
                "卡玛": round(m["calmar"], 2),
                "换手": m["n_trades"],
                "成本%": round(m["total_cost"], 1),
                "仓位%": round(m["exposure"] * 100, 0),
            })
        sweep_df = pd.DataFrame(rows)
        print(sweep_df.to_string(index=False))
        print(f"\n买入持有基准: 总收益 {bh_metrics['total_return']:+.1f}% | "
              f"年化 {bh_metrics['annual_return']:+.2f}% | 回撤 {bh_metrics['max_drawdown']:.1f}% | "
              f"夏普 {bh_metrics['sharpe']:.2f}")
        # 选择夏普最高的组合出图
        best_key = max(results, key=lambda k: results[k]["metrics"]["sharpe"])
        result = results[best_key]
        m = result["metrics"]
        print(f"\n最佳组合(按夏普): 买>{best_key[0]} 卖<{best_key[1]} 持{best_key[2]}天 -> "
              f"总收益 {m['total_return']:+.1f}% 夏普 {m['sharpe']:.2f}")
    else:
        cfg = (args.enter_threshold, args.exit_threshold, args.min_holding)
        result = results[cfg]
        m = result["metrics"]
        print(f"(买入阈值={m['enter_thr']}, 卖出阈值={m['exit_thr']}, 最短持有={m['min_holding']}天)")
        print(f"策略(Alpha158+LGBM):  {fmt_metrics(m)}")
        print(f"买入持有(benchmark):  {fmt_metrics(bh_metrics)}")
        print(f"  - 胜率(持仓日) {m['win_rate']*100:.1f}% | 平均持仓 {m['avg_holding']:.0f} 天 | "
              f"交易成本合计 {m['total_cost']:.2f}%")

        # 分年绩效
        ret_s = result["net_ret"]
        print("\n分年度收益 (策略 vs 买入持有):")
        ann = pd.DataFrame({
            "策略": ret_s.groupby(ret_s.index.year).apply(lambda x: (1 + x).prod() - 1) * 100,
            "买入持有": close.pct_change().groupby(close.pct_change().index.year).apply(lambda x: (1 + x).prod() - 1) * 100,
        })
        print(ann.round(2).to_string())

    # 5. 输出
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = REPORT_DIR / f"backtest_alpha158_{args.symbol.replace('.', '_')}_{ts}.csv"
    png_path = REPORT_DIR / f"backtest_alpha158_{args.symbol.replace('.', '_')}_{ts}.png"

    out = pd.DataFrame({
        "date": idx,
        "close": close.values,
        "pos": result["pos"].values,
        "ret": close.pct_change().values,
        "strat_ret": result["strat_ret"].values,
        "cost": result["cost"].values,
        "net_ret": result["net_ret"].values,
        "equity": result["equity"].values,
        "bh_equity": (close / close.iloc[0]).values,
    })
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\nCSV: {csv_path} ({len(out)} 行)")
    print(f"结论: 策略累计 {m['total_return']:+.2f}% vs 买入持有 {bh_metrics['total_return']:+.2f}%")

    plot_result(close, result, bh_metrics, png_path,
                f"招商银行 {args.symbol} Alpha158 十年回测 (买>{m['enter_thr']} 卖<{m['exit_thr']} "
                f"持有{m['min_holding']}天)")
    print(f"图: {png_path}")


if __name__ == "__main__":
    main()
