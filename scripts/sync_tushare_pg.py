"""从 Tushare 同步 日线 + 财务数据 到本地 PostgreSQL。

参考 ashare-lowfreq-research 的 sync-tushare-sqlite 流程, 落地为「Tushare -> PostgreSQL」:
  - 日线: 前复权因子/换手率/成交均价, 落表 stock_daily_bars
  - 财务: fina_indicator 核心指标(每报告期一行), 落表 stock_financial

用法 (需在 .venv 下运行, 内含 qlib/lightgbm/asyncpg/tushare):
  .venv/bin/python scripts/sync_tushare_pg.py                        # 招商银行 全历史
  .venv/bin/python scripts/sync_tushare_pg.py --codes 000858.SZ,600519.SH
  .venv/bin/python scripts/sync_tushare_pg.py --start 20130101 --end 20260812

约定:
  - 幂等 upsert (symbol+trade_date / symbol+end_date 唯一), 可重复运行续跑
  - TUSHARE_TOKEN / DATABASE_URL 从根目录 .env 读取
  - 单位: 价格为元, vol=手, amount=千元, vwap=元/股(=amount*1000/(vol*100))
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import asyncpg
import tushare as ts

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """加载根目录 .env (轻量 dotenv, 不覆盖已有环境变量)。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def dsn() -> str:
    return (
        os.environ.get("DATABASE_URL", "postgresql://huangyong@localhost:5432/llm_caibao")
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres+asyncpg://", "postgres://")
    )


DAILY_DDL = """
CREATE TABLE IF NOT EXISTS stock_daily_bars (
    id            BIGSERIAL PRIMARY KEY,
    symbol        VARCHAR(16)  NOT NULL,
    trade_date    DATE         NOT NULL,
    open          NUMERIC(14,4),
    high          NUMERIC(14,4),
    low           NUMERIC(14,4),
    close         NUMERIC(14,4),
    pre_close     NUMERIC(14,4),
    pct_chg       NUMERIC(10,4),
    vol           NUMERIC(18,4),   -- 成交量(手)
    amount        NUMERIC(20,4),   -- 成交额(千元)
    vwap          NUMERIC(14,4),   -- 成交均价(元) = amount*1000/(vol*100)
    adj_factor    NUMERIC(14,4),   -- 复权因子
    turnover_rate NUMERIC(10,4),   -- 换手率(%)
    UNIQUE (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_stock_daily_symbol_date
    ON stock_daily_bars (symbol, trade_date);
"""

FINANCIAL_DDL = """
CREATE TABLE IF NOT EXISTS stock_financial (
    id                BIGSERIAL PRIMARY KEY,
    symbol            VARCHAR(16) NOT NULL,
    end_date          DATE        NOT NULL,  -- 报告期
    ann_date          DATE,                  -- 公告日期
    eps               NUMERIC(14,4),         -- 每股收益
    bps               NUMERIC(14,4),         -- 每股净资产
    ocfps             NUMERIC(14,4),         -- 每股经营现金流
    roe               NUMERIC(10,4),         -- 净资产收益率(加权)
    roe_waa           NUMERIC(10,4),
    roa               NUMERIC(10,4),         -- 总资产收益率
    netprofit_margin  NUMERIC(10,4),         -- 净利率
    grossprofit_margin NUMERIC(10,4),        -- 毛利率
    debt_to_assets    NUMERIC(10,4),         -- 资产负债率
    assets_to_eqt     NUMERIC(10,4),         -- 权益乘数
    or_yoy            NUMERIC(10,4),         -- 营收同比
    netprofit_yoy     NUMERIC(10,4),         -- 净利同比
    op_yoy            NUMERIC(10,4),         -- 营业利润同比
    ebt_yoy           NUMERIC(10,4),         -- 利润总额同比
    ocf_yoy           NUMERIC(10,4),         -- 经营现金流同比
    assets_yoy        NUMERIC(10,4),         -- 总资产同比
    equity_yoy        NUMERIC(10,4),         -- 净资产同比
    q_gr_yoy          NUMERIC(10,4),         -- 营收单季同比
    q_np_yoy          NUMERIC(10,4),         -- 净利单季同比
    current_ratio     NUMERIC(10,4),         -- 流动比率
    quick_ratio       NUMERIC(10,4),         -- 速动比率
    assets_turn       NUMERIC(10,4),         -- 总资产周转率
    inv_turn          NUMERIC(10,4),         -- 存货周转率
    arturn            NUMERIC(10,4),         -- 应收周转率
    ocf_to_or         NUMERIC(10,4),         -- 销售现金含量
    rd_exp_ratio      NUMERIC(10,4),         -- 研发费用率
    fcff              NUMERIC(20,4),         -- 自由现金流(元)
    UNIQUE (symbol, end_date)
);
CREATE INDEX IF NOT EXISTS idx_stock_financial_symbol_date
    ON stock_financial (symbol, end_date);
"""

# fina_indicator 字段 -> 列名 (列名即 PG 列名, 过滤不存在的字段)
_FIN_FIELDS = [
    "ann_date", "end_date", "eps", "bps", "ocfps", "roe", "roe_waa", "roa",
    "netprofit_margin", "grossprofit_margin", "debt_to_assets", "assets_to_eqt",
    "or_yoy", "netprofit_yoy", "op_yoy", "ebt_yoy", "ocf_yoy", "assets_yoy",
    "equity_yoy", "q_gr_yoy", "q_np_yoy", "current_ratio", "quick_ratio",
    "assets_turn", "inv_turn", "arturn", "ocf_to_or", "rd_exp_ratio", "fcff",
]

_FIN_INSERT = """
INSERT INTO stock_financial (symbol, {cols})
VALUES ($1, {ph})
ON CONFLICT (symbol, end_date) DO UPDATE SET
    {updates}
"""

_DAILY_INSERT = """
INSERT INTO stock_daily_bars (
    symbol, trade_date, open, high, low, close, pre_close, pct_chg,
    vol, amount, vwap, adj_factor, turnover_rate
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
ON CONFLICT (symbol, trade_date) DO UPDATE SET
    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
    pre_close=EXCLUDED.pre_close, pct_chg=EXCLUDED.pct_chg, vol=EXCLUDED.vol,
    amount=EXCLUDED.amount, vwap=EXCLUDED.vwap, adj_factor=EXCLUDED.adj_factor,
    turnover_rate=EXCLUDED.turnover_rate
"""


def _to_date(v: object) -> object:
    if v is None or (isinstance(v, float) and v != v):  # NaN
        return None
    if isinstance(v, datetime):
        return v.date()
    s = str(v)
    if not s or s == "nan":
        return None
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _num(v: object) -> object:
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f or f in (float("inf"), float("-inf")) else f
    except (TypeError, ValueError):
        return None


def sync_daily(pro, symbol: str, start: str, end: str, sleep: float) -> int:
    """同步一只股票的日线(含复权因子/换手率/成交均价)。返回行数。"""
    daily = pro.daily(ts_code=symbol, start_date=start, end_date=end)
    if daily is None or daily.empty:
        return 0
    daily = daily.sort_values("trade_date")

    adj = pro.adj_factor(ts_code=symbol, start_date=start, end_date=end)
    adj_map = {r["trade_date"]: _num(r["adj_factor"]) for _, r in adj.iterrows()} if adj is not None else {}
    time.sleep(sleep)

    basic = pro.daily_basic(ts_code=symbol, start_date=start, end_date=end,
                            fields="trade_date,turnover_rate")
    tr_map = {r["trade_date"]: _num(r["turnover_rate"]) for _, r in basic.iterrows()} if basic is not None else {}

    rows = []
    for _, r in daily.iterrows():
        td = r["trade_date"]
        vol = _num(r.get("vol"))
        amount = _num(r.get("amount"))
        vwap = (amount * 10.0 / vol) if (vol and amount and vol > 0) else None
        rows.append((
            symbol,
            _to_date(td),
            _num(r.get("open")), _num(r.get("high")), _num(r.get("low")),
            _num(r.get("close")), _num(r.get("pre_close")), _num(r.get("pct_chg")),
            vol, amount, vwap,
            adj_map.get(td),
            tr_map.get(td),
        ))
    return rows


def sync_financial(pro, symbol: str, start: str, end: str, sleep: float) -> tuple[list, list]:
    """同步一只股票的财务指标(fina_indicator 每报告期一行)。

    返回 (rows, cols): rows 每行首元素为 symbol, 后续按 cols 顺序。
    """
    df = pro.fina_indicator(ts_code=symbol, start_date=start, end_date=end)
    if df is None or df.empty:
        return [], []
    time.sleep(sleep)
    cols = [c for c in _FIN_FIELDS if c in df.columns]
    rows = []
    for _, r in df.iterrows():
        row = [symbol]
        for c in cols:
            if c in ("ann_date", "end_date"):
                row.append(_to_date(r.get(c)))
            else:
                row.append(_num(r.get(c)))
        rows.append(tuple(row))
    return rows, cols


async def create_tables(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(DAILY_DDL)
        await conn.execute(FINANCIAL_DDL)


async def upsert_daily(pool, rows) -> int:
    if not rows:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(_DAILY_INSERT, rows)
    return len(rows)


async def upsert_financial(pool, symbol, rows, cols) -> int:
    if not rows:
        return 0
    cols_str = ", ".join(cols)
    ph = ", ".join(f"${i}" for i in range(2, len(cols) + 2))
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
    sql = _FIN_INSERT.format(cols=cols_str, ph=ph, updates=updates)
    async with pool.acquire() as conn:
        await conn.executemany(sql, rows)  # 每行已含 symbol 首位
    return len(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Tushare 日线+财务 同步到 PostgreSQL")
    parser.add_argument("--codes", default="600036.SH", help="股票代码(逗号分隔), 默认招商银行")
    parser.add_argument("--start", default=None, help="起始日 YYYYMMDD, 空=自动(上市日起)")
    parser.add_argument("--end", default=None, help="结束日 YYYYMMDD, 空=今天")
    parser.add_argument("--sleep", type=float, default=0.15, help="tushare 调用间隔秒数")
    parser.add_argument("--only-daily", action="store_true", help="只同步日线")
    parser.add_argument("--only-financial", action="store_true", help="只同步财务")
    args = parser.parse_args()

    load_env()
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        sys.exit("缺少 TUSHARE_TOKEN, 请检查根目录 .env")
    ts.set_token(token)
    pro = ts.pro_api()

    end = args.end or date.today().strftime("%Y%m%d")
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    pool = await asyncpg.create_pool(dsn=dsn(), min_size=1, max_size=5)
    try:
        await create_tables(pool)

        for symbol in codes:
            print(f"\n=== {symbol} ===")
            # 上市日期作为默认起点
            start = args.start
            if not start:
                basic = pro.stock_basic(ts_code=symbol, fields="ts_code,list_date")
                start = str(basic.iloc[0]["list_date"]) if basic is not None and not basic.empty else "20000101"
                time.sleep(args.sleep)
            print(f"区间: {start} ~ {end}")

            if not args.only_financial:
                t0 = time.time()
                rows = sync_daily(pro, symbol, start, end, args.sleep)
                n = await upsert_daily(pool, rows)
                print(f"日线: {n} 行, 耗时 {time.time()-t0:.1f}s")

            if not args.only_daily:
                t0 = time.time()
                fin, fin_cols = sync_financial(pro, symbol, start, end, args.sleep)
                n = await upsert_financial(pool, symbol, fin, fin_cols)
                print(f"财务: {n} 行, 耗时 {time.time()-t0:.1f}s")
    finally:
        await pool.close()

    print("\n完成.")


if __name__ == "__main__":
    asyncio.run(main())
