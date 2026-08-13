"""Alpha158 因子回测服务 (qlib Alpha158 + LightGBM + 长/空策略)。

为前端「Alpha158 回测」tab 提供完整链路:
  1. 股票池数据保障: 缺失股票从 tushare 同步到 PostgreSQL (stock_daily_bars)
  2. qlib 数据构建: 多股共享交易日历, 导出 storage/qlib_data/cn_data (前复权)
  3. Alpha158 因子: qlib.contrib.data.handler.Alpha158 (158 个量价因子)
  4. Pooled 横截面 walk-forward LightGBM 训练与预测
  5. 单标的 长/空 回测 + 等权组合聚合

用法 (由 api/main.py 调用, 也可被 scripts/ 复用):
  result = await alpha158_service.backtest(symbols, {...params})
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QLIB_DIR = ROOT / "storage" / "qlib_data" / "cn_data"

# --- A 股交易成本 ---
COMMISSION = 0.00025    # 佣金 万2.5 (双边)
STAMP_DUTY = 0.0005     # 印花税 0.05% (仅卖出)
TRANSFER_FEE = 0.00001  # 过户费 0.001% (双边)
BUY_COST = COMMISSION + TRANSFER_FEE
SELL_COST = COMMISSION + STAMP_DUTY + TRANSFER_FEE

_QLIB_FIELDS = ["open", "high", "low", "close", "vwap", "volume"]

# qlib.init 为全局状态, 用锁串行化「数据构建 + 因子 + 训练 + 回测」整体流程
_backtest_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 环境 / 数据库
# ---------------------------------------------------------------------------

def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _dsn() -> str:
    return (
        os.environ.get("DATABASE_URL", "postgresql://huangyong@localhost:5432/llm_caibao")
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres+asyncpg://", "postgres://")
    )


async def _get_pool():
    import asyncpg

    _load_env()
    return await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=5)


# ---------------------------------------------------------------------------
# qlib 数据目录构建 (多股共享日历)
# ---------------------------------------------------------------------------

def _write_bin(path: Path, values: np.ndarray, start_index: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.hstack([np.float32(start_index), np.asarray(values).astype("<f4")]).tofile(path)


def _write_calendar(path: Path, dates: list[date]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(d.strftime("%Y-%m-%d") for d in dates) + "\n", encoding="utf-8")


def _write_instruments(path: Path, instruments: dict) -> None:
    """instruments: {symbol: (start_date, end_date)}"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{sym}\t{s.strftime('%Y-%m-%d')}\t{e.strftime('%Y-%m-%d')}"
             for sym, (s, e) in sorted(instruments.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def build_qlib_dataset(symbols: list[str], qlib_dir: Path = DEFAULT_QLIB_DIR,
                             start: str = "20160101", end: str = "",
                             adj: str = "qfq") -> dict:
    """从 PostgreSQL stock_daily_bars 构建多股 qlib 数据目录。

    共享交易日历 = 所有股票交易日的并集; 个股缺日写 NaN。
    返回: {qlib_dir, calendar_days, instruments: {sym: {start,end,rows,latest_adj}}, skipped}
    """
    qlib_dir = Path(qlib_dir)
    if qlib_dir.exists():
        import shutil
        shutil.rmtree(qlib_dir)
    end = end or date.today().strftime("%Y%m%d")
    d0 = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:8]}")
    d1 = date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:8]}")

    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT symbol, trade_date, open, high, low, close, vwap, vol, adj_factor "
                "FROM stock_daily_bars WHERE symbol = ANY($1::text[]) "
                "AND trade_date BETWEEN $2 AND $3 ORDER BY symbol, trade_date",
                list(symbols), d0, d1,
            )
    finally:
        await pool.close()

    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)

    all_dates: set[date] = set()
    instruments: dict[str, tuple[date, date]] = {}
    skipped: list[dict] = []
    ok_symbols: list[str] = []
    for sym in symbols:
        srows = by_sym.get(sym, [])
        if len(srows) < 60:  # 至少 60 个交易日, 否则因子无法计算
            skipped.append({"ts_code": sym, "reason": f"数据不足({len(srows)} 日)"})
            continue
        ok_symbols.append(sym)
        ds = [r["trade_date"] for r in srows]
        all_dates.update(ds)
        instruments[sym] = (min(ds), max(ds))

    if not ok_symbols:
        return {"qlib_dir": str(qlib_dir), "calendar_days": 0, "instruments": {},
                "skipped": skipped}

    calendar = sorted(all_dates)
    _write_calendar(qlib_dir / "calendars" / "day.txt", calendar)
    _write_instruments(qlib_dir / "instruments" / "all.txt", instruments)
    pos = {d: i for i, d in enumerate(calendar)}
    n = len(calendar)
    summary = {}

    for sym in ok_symbols:
        srows = by_sym[sym]
        row_map = {r["trade_date"]: r for r in srows}
        latest_adj = max((float(r["adj_factor"]) for r in srows if r["adj_factor"]), default=1.0)
        values: dict[str, np.ndarray] = {f: np.full(n, np.nan) for f in _QLIB_FIELDS}
        for d, r in row_map.items():
            i = pos[d]
            s = float(r["adj_factor"]) / latest_adj if (adj == "qfq" and r["adj_factor"]) else 1.0
            values["open"][i] = float(r["open"]) * s if r["open"] is not None else np.nan
            values["high"][i] = float(r["high"]) * s if r["high"] is not None else np.nan
            values["low"][i] = float(r["low"]) * s if r["low"] is not None else np.nan
            values["close"][i] = float(r["close"]) * s if r["close"] is not None else np.nan
            values["vwap"][i] = (float(r["vwap"]) * s if r["vwap"] else np.nan)
            values["volume"][i] = float(r["vol"]) if r["vol"] else 0.0
        feat_dir = qlib_dir / "features" / sym.lower()
        for f in _QLIB_FIELDS:
            _write_bin(feat_dir / f"{f}.day.bin", values[f], start_index=0)
        summary[sym] = {"start": str(min(row_map)), "end": str(max(row_map)),
                        "rows": len(srows), "latest_adj": latest_adj}

    return {"qlib_dir": str(qlib_dir), "calendar_days": n, "instruments": summary,
            "skipped": skipped}


# ---------------------------------------------------------------------------
# 数据保障: 缺失股票从 tushare 同步 (同步 tushare, 在线程中调用)
# ---------------------------------------------------------------------------

def _sync_one_sync(symbol: str, start: str, end: str, sleep: float = 0.1) -> list:
    """同步单只股票日线(含复权因子/换手率/成交均价)到 stock_daily_bars, 返回行列表。"""
    import time
    import tushare as ts

    _load_env()
    ts.set_token(os.environ.get("TUSHARE_TOKEN", ""))
    pro = ts.pro_api()
    daily = pro.daily(ts_code=symbol, start_date=start, end_date=end)
    if daily is None or daily.empty:
        return []
    daily = daily.sort_values("trade_date")
    time.sleep(sleep)
    adj = pro.adj_factor(ts_code=symbol, start_date=start, end_date=end)
    adj_map = {r["trade_date"]: float(r["adj_factor"])
               for _, r in adj.iterrows()} if adj is not None else {}
    time.sleep(sleep)
    basic = pro.daily_basic(ts_code=symbol, start_date=start, end_date=end,
                            fields="trade_date,turnover_rate")
    tr_map = {r["trade_date"]: float(r["turnover_rate"])
              for _, r in basic.iterrows()} if basic is not None else {}

    def _d(v):
        if v is None:
            return None
        s = str(v)
        return datetime.strptime(s[:8], "%Y%m%d").date() if s else None

    def _n(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if f != f else f
        except (TypeError, ValueError):
            return None

    rows = []
    for _, r in daily.iterrows():
        vol = _n(r.get("vol"))
        amount = _n(r.get("amount"))
        vwap = (amount * 10.0 / vol) if (vol and amount and vol > 0) else None
        rows.append((symbol, _d(r["trade_date"]), _n(r.get("open")), _n(r.get("high")),
                     _n(r.get("low")), _n(r.get("close")), _n(r.get("pre_close")),
                     _n(r.get("pct_chg")), vol, amount, vwap, adj_map.get(r["trade_date"]),
                     tr_map.get(r["trade_date"])))
    return rows


async def _ensure_stock_data(symbols: list[str], start: str, end: str) -> tuple[list[str], list[dict]]:
    """确保股票池在 [start, end] 有数据; 缺失则同步。返回 (ok_symbols, skipped)。"""
    pool = await _get_pool()
    ok: list[str] = []
    skipped: list[dict] = []
    try:
        async with pool.acquire() as conn:
            have = await conn.fetch(
                "SELECT symbol, count(*) n, min(trade_date) mn, max(trade_date) mx "
                "FROM stock_daily_bars WHERE symbol = ANY($1::text[]) "
                "AND trade_date >= $2 GROUP BY symbol", list(symbols),
                date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:8]}"))
        counts = {r["symbol"]: r for r in have}
        need_sync = []
        for sym in symbols:
            c = counts.get(sym)
            if c and c["n"] >= 60:
                ok.append(sym)
            else:
                need_sync.append(sym)
        # 同步缺失股票 (在线程中, 避免阻塞事件循环; 并发上限=3 防 tushare 限频)
        if need_sync:
            sem = asyncio.Semaphore(3)

            async def _sync_one(sym):
                async with sem:
                    return await asyncio.to_thread(_sync_one_sync, sym, start, end)

            results = await asyncio.gather(*(_sync_one(sym) for sym in need_sync))
            rows_all = [row for r in results if isinstance(r, list) for row in r]
            if rows_all:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        "INSERT INTO stock_daily_bars (symbol, trade_date, open, high, low, "
                        "close, pre_close, pct_chg, vol, amount, vwap, adj_factor, turnover_rate) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
                        "ON CONFLICT (symbol, trade_date) DO UPDATE SET "
                        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
                        "close=EXCLUDED.close, pre_close=EXCLUDED.pre_close, "
                        "pct_chg=EXCLUDED.pct_chg, vol=EXCLUDED.vol, amount=EXCLUDED.amount, "
                        "vwap=EXCLUDED.vwap, adj_factor=EXCLUDED.adj_factor, "
                        "turnover_rate=EXCLUDED.turnover_rate",
                        [tuple(r) for r in rows_all])
            for sym, rows in zip(need_sync, results):
                n = len(rows) if isinstance(rows, list) else 0
                if n >= 60:
                    ok.append(sym)
                else:
                    skipped.append({"ts_code": sym, "reason": f"无日线数据({n} 行)"})
    finally:
        await pool.close()
    return ok, skipped


# ---------------------------------------------------------------------------
# Alpha158 因子 + Pooled walk-forward + 回测 (同步, 在线程中执行)
# ---------------------------------------------------------------------------

def _load_panel(handler) -> tuple[pd.DataFrame, list[str]]:
    df = handler.fetch().reset_index()  # index -> instrument/date 列
    df = df.rename(columns={"datetime": "date"})
    feat_cols = [c for c in df.columns if c not in ("instrument", "date", "LABEL0")]
    return df, feat_cols


def _walk_forward_pooled(df: pd.DataFrame, feat_cols: list[str], symbols: list[str],
                         chunks: list[tuple], train_start, gap_days: int = 30) -> dict[str, pd.Series]:
    """Pooled 横截面 walk-forward: 每段用全部股票训练 LightGBM, 预测各股票。"""
    import lightgbm as lgb

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    pred_parts: dict[str, list] = {s: [] for s in symbols}
    for ts, te in chunks:
        train_end = ts - pd.Timedelta(days=gap_days)
        train = df[(df["date"] >= train_start) & (df["date"] <= train_end)].dropna(subset=["LABEL0"])
        test = df[(df["date"] >= ts) & (df["date"] < te)]
        if len(train) < 200:
            continue
        model = lgb.LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=31,
            subsample=0.8, colsample_bytree=0.8, min_child_samples=20,
            random_state=42, verbose=-1, n_jobs=4,
        )
        model.fit(train[feat_cols], train["LABEL0"].astype(float))
        test = test.copy()
        test["pred"] = model.predict(test[feat_cols])
        for inst, g in test.groupby("instrument"):
            pred_parts[inst].append(g[["date", "pred"]])
    out = {}
    for s in symbols:
        parts = pred_parts.get(s, [])
        if not parts:
            out[s] = pd.Series(dtype=float)
            continue
        ser = pd.concat(parts).drop_duplicates("date").set_index("date")["pred"].sort_index()
        out[s] = ser
    return out


def _calc_metrics(net_ret: pd.Series, capital: float) -> dict:
    if net_ret.empty:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
                "calmar": 0.0, "sharpe": 0.0, "final_value": capital, "years": 0.0}
    net_ret = net_ret.fillna(0.0)
    equity = (1 + net_ret).cumprod() * capital
    final = float(equity.iloc[-1])
    years = max((net_ret.index[-1] - net_ret.index[0]).days / 365.25, 1e-9)
    total_ret = (final / capital - 1) * 100
    ann_ret = (pow(final / capital, 1 / years) - 1) * 100 if final > 0 else 0.0
    mdd = float(((equity - equity.cummax()) / equity.cummax() * 100).min())
    std = net_ret.std(ddof=1)
    ann_vol = std * np.sqrt(252)
    sharpe = (ann_ret / 100) / ann_vol if ann_vol > 0 else 0.0
    calmar = ann_ret / mdd if mdd > 0 else 0.0
    return {"total_return": total_ret, "annual_return": ann_ret, "max_drawdown": mdd,
            "calmar": calmar, "sharpe": sharpe, "final_value": final, "years": years}


def _run_backtest(close: pd.Series, pred: pd.Series, enter_thr: float,
                  exit_thr: float, min_holding: int, capital: float) -> dict:
    """长/空回测: 决策于当日收盘, 次日生效; 扣除交易成本。"""
    close = close.astype(float)
    ret = close.pct_change()
    pred_s = pred.reindex(close.index)
    holding, hold_days = 0, 0
    pos_arr = np.zeros(len(close))
    for i in range(len(close)):
        p = pred_s.iloc[i]
        if holding == 0 and p > enter_thr:
            holding, hold_days = 1, 0
        elif holding == 1:
            hold_days += 1
            if hold_days >= min_holding and p < exit_thr:
                holding = 0
        pos_arr[i] = holding
    pos = pd.Series(pos_arr, index=close.index).shift(1).fillna(0.0)
    buy_turn = pos.diff().clip(lower=0).fillna(0.0)
    sell_turn = (-pos.diff()).clip(lower=0).fillna(0.0)
    cost = buy_turn * BUY_COST + sell_turn * SELL_COST
    strat_ret = pos * ret
    net_ret = strat_ret - cost
    m = _calc_metrics(net_ret, capital)
    m.update({
        "exposure": float(pos.mean()),
        "n_trades": int((buy_turn > 0).sum()),
        "win_rate": float((strat_ret[pos > 0] > 0).mean()) if (pos > 0).any() else 0.0,
        "avg_holding": float(pos.mean() * len(pos) / max((buy_turn > 0).sum(), 1)),
        "total_cost": float(cost.sum() * 100),
    })
    return {"pos": pos, "net_ret": net_ret, "cost": cost, "metrics": m}


def _aggregate_curve(stock_results: dict, close_by_sym: dict, capital: float) -> dict:
    """等权组合: 每个交易日取各股票当日收益的均值 (等权日再平衡口径)。"""
    dates = sorted(set().union(*[r["net_ret"].dropna().index.tolist() for r in stock_results.values()]))
    if not dates:
        return {}
    idx = pd.DatetimeIndex(dates)
    strat = pd.DataFrame({s: r["net_ret"].reindex(idx) for s, r in stock_results.items()}).fillna(0.0)
    bh = pd.DataFrame({s: close_by_sym[s].pct_change().reindex(idx) for s in close_by_sym}).fillna(0.0)
    s_ret = strat.mean(axis=1)
    b_ret = bh.mean(axis=1)
    s_eq = (1 + s_ret).cumprod() * capital
    b_eq = (1 + b_ret).cumprod() * capital
    sm = _calc_metrics(s_ret, capital)
    bm = _calc_metrics(b_ret, capital)
    dd = (s_eq / s_eq.cummax() - 1) * 100
    return {
        "dates": [d.strftime("%Y%m%d") for d in idx],
        "strategy": [round(float(v), 4) for v in (s_eq / capital - 1) * 100],
        "buyhold": [round(float(v), 4) for v in (b_eq / capital - 1) * 100],
        "drawdown": [round(float(v), 4) for v in dd],
        "strategy_metrics": sm,
        "buyhold_metrics": bm,
    }


def _run_alpha158(qlib_dir, symbols, params) -> dict:
    """同步执行: qlib 初始化 + 因子 + walk-forward + 回测 + 聚合。"""
    import qlib
    from qlib.contrib.data.handler import Alpha158
    from qlib.data import D
    from qlib.data.dataset.handler import DataHandlerLP

    data_start, data_end = params["data_start"], params["test_end"]
    enter_thr, exit_thr, mh = params["enter_threshold"], params["exit_threshold"], params["min_holding"]
    capital = params["initial_capital"]

    qlib.init(provider_uri=qlib_dir, region="cn")
    handler = Alpha158(
        instruments="all", start_time=data_start, end_time=data_end, freq="day",
        infer_processors=[], learn_processors=[],
        process_type=DataHandlerLP.PTYPE_A,
    )
    df, feat_cols = _load_panel(handler)
    df = df[df["instrument"].isin(symbols)]
    df["date"] = pd.to_datetime(df["date"])

    # walk-forward 分段 (年度)
    test_start = pd.Timestamp(params["test_start"])
    # test_end 为空("最新")时取数据实际最大交易日 (pd.Timestamp("")=NaT 会导致分段为空)
    test_end = pd.Timestamp(params["test_end"]) if params["test_end"] else df["date"].max()
    chunks = []
    cur = test_start
    while cur < test_end:
        nxt = min(cur + pd.DateOffset(years=1), test_end)
        chunks.append((cur, nxt))
        cur = nxt

    train_start = pd.Timestamp(params["train_start"])
    preds = _walk_forward_pooled(df, feat_cols, symbols, chunks, train_start)

    # 各股票回测
    stock_results = {}
    close_by_sym = {}
    stocks_meta = []
    for sym in symbols:
        close = D.features([sym], ["$close"], start_time=params["test_start"],
                           end_time=test_end.strftime("%Y%m%d"), freq="day")["$close"]
        if isinstance(close.index, pd.MultiIndex):
            close = close.droplevel("instrument")
        close.index = pd.to_datetime(close.index)
        close = close.rename(sym)
        pred = preds.get(sym, pd.Series(dtype=float))
        idx = close.index.intersection(pred.index)
        if len(idx) < 60:
            stocks_meta.append({"ts_code": sym, "ok": False, "reason": "预测数据不足"})
            continue
        close = close.loc[idx]
        pred = pred.loc[idx]
        r = _run_backtest(close, pred, enter_thr, exit_thr, mh, capital)
        m = r["metrics"]
        # 分年度
        annual = ((1 + r["net_ret"]).groupby(r["net_ret"].index.year).prod() - 1) * 100
        stock_results[sym] = r
        close_by_sym[sym] = close
        stocks_meta.append({
            "ts_code": sym,
            "ok": True,
            "metrics": {k: round(float(m[k]), 4) for k in
                        ("total_return", "annual_return", "max_drawdown", "sharpe",
                         "calmar", "final_value", "exposure", "n_trades", "win_rate",
                         "avg_holding", "total_cost")},
            "annual": {str(k): round(float(v), 2) for k, v in annual.items()},
            "start": str(idx.min().date()), "end": str(idx.max().date()),
            "days": len(idx),
        })

    portfolio = _aggregate_curve(stock_results, close_by_sym, capital) if stock_results else {}
    return {"stocks": stocks_meta, "portfolio": portfolio, "feature_count": len(feat_cols)}


# ---------------------------------------------------------------------------
# 对外异步入口
# ---------------------------------------------------------------------------

async def backtest(symbols: list[str], params: dict) -> dict:
    """完整链路: 数据保障 -> qlib 构建 -> Alpha158 -> 回测。

    params 关键字段:
      data_start / train_start / test_start / test_end (YYYYMMDD)
      enter_threshold / exit_threshold / min_holding / initial_capital
    """
    _load_env()
    async with _backtest_lock:
        # 1. 数据保障 (缺失同步)
        ok_symbols, skipped = await _ensure_stock_data(symbols, params["data_start"], params["test_end"])
        if not ok_symbols:
            return {"ok": False, "error": "没有可用股票(数据不足)", "skipped": skipped,
                    "stocks": [], "portfolio": {}}
        # 2. 构建 qlib 数据目录
        qlib_dir = Path(params.get("qlib_dir", str(DEFAULT_QLIB_DIR)))
        built = await build_qlib_dataset(ok_symbols, qlib_dir, params["data_start"], params["test_end"])
        ok_symbols = [s for s in ok_symbols if s in built["instruments"]]
        skipped += built["skipped"]
        if not ok_symbols:
            return {"ok": False, "error": "qlib 数据构建失败", "skipped": skipped,
                    "stocks": [], "portfolio": {}}
        # 3. Alpha158 + 回测 (CPU 密集, 在线程中)
        result = await asyncio.to_thread(_run_alpha158, built["qlib_dir"], ok_symbols, params)
    result["ok"] = True
    result["skipped"] = skipped
    result["params"] = params
    result["pool"] = built["instruments"]
    return result
