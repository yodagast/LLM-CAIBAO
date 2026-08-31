"""PostgreSQL 存取层: 红利低波选股数据表 red_low_vol。

连接串从根目录 .env 的 DATABASE_URL 读取 (默认本机 Postgres.app)。
表按 (ts_code, year) 唯一, 数据通过 upsert 幂等写入。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# asyncpg 全局连接池 (运行时用)
_pool: asyncpg.Pool | None = None

# 允许前端排序的字段白名单 (防 SQL 注入)
SORTABLE_COLUMNS = {
    "year": "year",
    "name": "name",
    "dividend_yield": "dividend_yield",
    "dividend_yield_ttm": "dividend_yield_ttm",
    "volatility": "volatility",
    "div_per_share": "div_per_share",
    "free_cashflow": "free_cashflow",
    "gross_margin": "gross_margin",
    "eps": "eps",
    "payout_ratio": "payout_ratio",
    "roe": "roe",
    "debt_to_assets": "debt_to_assets",
    "avg_daily_mv": "avg_daily_mv",
    "avg_daily_amt": "avg_daily_amt",
    "dividend_growth_3y": "dividend_growth_3y",
    "last_close": "last_close",
}

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS red_low_vol (
    id                    BIGSERIAL PRIMARY KEY,
    ts_code               VARCHAR(16)  NOT NULL,
    symbol                VARCHAR(8)   NOT NULL,
    name                  VARCHAR(64)  NOT NULL,
    industry              VARCHAR(32)  DEFAULT '',
    year                  INTEGER      NOT NULL,
    dividend_yield        DOUBLE PRECISION,   -- 静态股息率 % (当年每股分红 / 年末收盘价)
    dividend_yield_ttm    DOUBLE PRECISION,   -- 股息率-TTM % (当年每股分红 / 上个交易日收盘价)
    last_close            DOUBLE PRECISION,   -- 上个交易日收盘价 (股息率-TTM 分母)
    volatility            DOUBLE PRECISION,   -- 年化波动率 % (日收益率标准差 * sqrt(252))
    div_per_share         DOUBLE PRECISION,   -- 每股现金分红 (元)
    free_cashflow         DOUBLE PRECISION,   -- 企业自由现金流 (万元)
    gross_margin          DOUBLE PRECISION,   -- 毛利率 %
    eps                   DOUBLE PRECISION,   -- 每股收益 (元)
    payout_ratio          DOUBLE PRECISION,   -- 分红率 % (每股分红/每股收益)
    dividend_growth_3y    DOUBLE PRECISION,   -- 3 年每股股利复合增长率 %
    roe                   DOUBLE PRECISION,   -- 净资产收益率 %
    debt_to_assets        DOUBLE PRECISION,   -- 资产负债率 %
    avg_daily_mv          DOUBLE PRECISION,   -- 日均总市值 (万元)
    avg_daily_amt         DOUBLE PRECISION,   -- 日均成交金额 (万元)
    end_date              VARCHAR(16)  DEFAULT '',
    updated_at            TIMESTAMP    DEFAULT now(),
    UNIQUE (ts_code, year)
);
CREATE INDEX IF NOT EXISTS idx_rlv_ind_year ON red_low_vol (industry, year);
"""


def _load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def _dsn() -> str:
    _load_env()
    return os.getenv("DATABASE_URL", "postgresql://huangyong@localhost:5432/llm_caibao")


async def _get_pool() -> asyncpg.Pool:
    """获取全局 asyncpg 连接池 (惰性初始化)。"""
    global _pool
    if _pool is None:
        dsn = _dsn()
        # .env 中 DATABASE_URL 可能是 SQLAlchemy 风格 (postgresql+asyncpg://),
        # asyncpg 只认 postgresql:// / postgres:// 前缀, 需转换
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgres://")
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=5, max_size=20)
    return _pool


def _upsert_sql(table: str, cols: list[str], conflict: tuple[str, ...]) -> str:
    """生成 asyncpg 风格 ($1..$n 位置占位符) 的幂等 upsert SQL。"""
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in conflict)
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {updates}, updated_at = now();"
    )


# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------

async def init_schema() -> None:
    """创建 red_low_vol 表与索引 (幂等), 并对旧表迁移新增列。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_DDL)
        # 旧表迁移: 新增 股息率-TTM / 上个交易日收盘价 / 毛利率 列
        await conn.execute("ALTER TABLE red_low_vol ADD COLUMN IF NOT EXISTS dividend_yield_ttm DOUBLE PRECISION;")
        await conn.execute("ALTER TABLE red_low_vol ADD COLUMN IF NOT EXISTS last_close DOUBLE PRECISION;")
        await conn.execute("ALTER TABLE red_low_vol ADD COLUMN IF NOT EXISTS gross_margin DOUBLE PRECISION;")


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

_UPSERT_COLS = [
    "ts_code", "symbol", "name", "industry", "year",
    "dividend_yield", "dividend_yield_ttm", "last_close", "volatility", "div_per_share", "free_cashflow", "gross_margin", "eps",
    "payout_ratio", "dividend_growth_3y", "roe", "debt_to_assets",
    "avg_daily_mv", "avg_daily_amt", "end_date",
]

_UPSERT_SQL = _upsert_sql("red_low_vol", _UPSERT_COLS, ("ts_code", "year"))


async def upsert_rows(rows: list[dict]) -> int:
    """按 (ts_code, year) upsert 写入, 返回写入行数。"""
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_UPSERT_SQL, [tuple(r.get(c) for c in _UPSERT_COLS) for r in rows])
    return len(rows)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

async def count_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配, 与同步 str.contains 一致)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM red_low_vol WHERE industry LIKE $1 AND year = $2;",
            f"%{industry}%", year))


async def query_screen(industry: str, years: list[int], sort_by: str = "dividend_yield",
                       order: str = "desc", limit: int = 500,
                       filters: dict | None = None) -> list[dict]:
    """按行业+年份(可多个)查询全部公司, 支持字段阈值筛选, 按指定指标排序。

    filters 形如 {"dividend_yield": {"min": 5}, "volatility": {"max": 20}}
    或 {"dividend_yield": 5} (视为 min); 字段名经白名单校验防注入。
    """
    col = SORTABLE_COLUMNS.get(sort_by, "dividend_yield")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []

    def ph() -> str:
        return f"${len(params) + 1}"

    if industry:
        # 子串匹配, 与同步逻辑 str.contains 一致 (如输入"电力"匹配"新型电力")
        conds.append(f"industry LIKE {ph()}")
        params.append(f"%{industry}%")
    if years:
        conds.append(f"year = ANY({ph()}::int[])")
        params.append([int(y) for y in years])

    # 动态筛选条件
    for key, flt in (filters or {}).items():
        col_name = SORTABLE_COLUMNS.get(key)
        if col_name is None:
            continue
        if isinstance(flt, dict):
            mn, mx = flt.get("min"), flt.get("max")
        else:
            mn, mx = flt, None
        if mn is not None:
            conds.append(f"{col_name} >= {ph()}")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= {ph()}")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, year,
               dividend_yield, dividend_yield_ttm, last_close, volatility, div_per_share, free_cashflow, gross_margin, eps,
               payout_ratio, dividend_growth_3y, roe, debt_to_assets,
               avg_daily_mv, avg_daily_amt, end_date
        FROM red_low_vol
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT {ph()}::int;
    """
    params.append(int(limit))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 基本面选股表 fundamental_screen (ROE 杜邦拆分等)
# ---------------------------------------------------------------------------

FUNDAMENTAL_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS fundamental_screen (
    id                    BIGSERIAL PRIMARY KEY,
    ts_code               VARCHAR(16)  NOT NULL,
    symbol                VARCHAR(8)   NOT NULL,
    name                  VARCHAR(64)  NOT NULL,
    industry              VARCHAR(32)  DEFAULT '',
    year                  INTEGER      NOT NULL,
    close                 DOUBLE PRECISION,   -- 最近价格 (年末收盘价)
    roe                   DOUBLE PRECISION,   -- ROE %
    net_margin            DOUBLE PRECISION,   -- 净利润率 %
    assets_turn           DOUBLE PRECISION,   -- 总资产周转率
    equity_multiplier     DOUBLE PRECISION,   -- 权益乘数 = 总资产/归母权益
    gross_margin          DOUBLE PRECISION,   -- 毛利率 %
    free_cashflow         DOUBLE PRECISION,   -- 企业自由现金流 (万元)
    debt_to_assets        DOUBLE PRECISION,   -- 资产负债率 %
    total_cur_assets      DOUBLE PRECISION,   -- 流动资产 (万元)
    money_cap             DOUBLE PRECISION,   -- 货币资金/现金 (万元)
    invturn_days          DOUBLE PRECISION,   -- 存货周转天数
    arturn_days           DOUBLE PRECISION,   -- 应收账款周转天数
    end_date              VARCHAR(16)  DEFAULT '',
    updated_at            TIMESTAMP    DEFAULT now(),
    UNIQUE (ts_code, year)
);
CREATE INDEX IF NOT EXISTS idx_fs_ind_year ON fundamental_screen (industry, year);
"""

FUNDAMENTAL_SORTABLE_COLUMNS = {
    "year": "year",
    "name": "name",
    "close": "close",
    "roe": "roe",
    "net_margin": "net_margin",
    "assets_turn": "assets_turn",
    "equity_multiplier": "equity_multiplier",
    "gross_margin": "gross_margin",
    "free_cashflow": "free_cashflow",
    "debt_to_assets": "debt_to_assets",
    "total_cur_assets": "total_cur_assets",
    "money_cap": "money_cap",
    "invturn_days": "invturn_days",
    "arturn_days": "arturn_days",
}

_FUND_COLS = [
    "ts_code", "symbol", "name", "industry", "year",
    "close", "roe", "net_margin", "assets_turn", "equity_multiplier",
    "gross_margin", "free_cashflow", "debt_to_assets", "total_cur_assets", "money_cap",
    "invturn_days", "arturn_days", "end_date",
]

_FUND_UPSERT_SQL = _upsert_sql("fundamental_screen", _FUND_COLS, ("ts_code", "year"))


async def init_fundamental_schema() -> None:
    """创建 fundamental_screen 表与索引 (幂等), 并对旧表迁移新增列。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(FUNDAMENTAL_SCHEMA_DDL)
        # 旧表迁移: 新增 自由现金流 列
        await conn.execute("ALTER TABLE fundamental_screen ADD COLUMN IF NOT EXISTS free_cashflow DOUBLE PRECISION;")


async def upsert_fundamental_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_FUND_UPSERT_SQL, [tuple(r.get(c) for c in _FUND_COLS) for r in rows])
    return len(rows)


async def count_fundamental_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM fundamental_screen WHERE industry LIKE $1 AND year = $2;",
            f"%{industry}%", year))


async def query_fundamental(industry: str, years: list[int], sort_by: str = "roe",
                            order: str = "desc", limit: int = 1000,
                            filters: dict | None = None) -> list[dict]:
    """按行业+多年份查询基本面数据, 支持阈值筛选与排序。"""
    col = FUNDAMENTAL_SORTABLE_COLUMNS.get(sort_by, "roe")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []

    def ph() -> str:
        return f"${len(params) + 1}"

    if industry:
        # 子串匹配, 与同步逻辑 str.contains 一致
        conds.append(f"industry LIKE {ph()}")
        params.append(f"%{industry}%")
    if years:
        conds.append(f"year = ANY({ph()}::int[])")
        params.append([int(y) for y in years])
    for key, flt in (filters or {}).items():
        col_name = FUNDAMENTAL_SORTABLE_COLUMNS.get(key)
        if col_name is None:
            continue
        if isinstance(flt, dict):
            mn, mx = flt.get("min"), flt.get("max")
        else:
            mn, mx = flt, None
        if mn is not None:
            conds.append(f"{col_name} >= {ph()}")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= {ph()}")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, year, close, roe, net_margin,
               assets_turn, equity_multiplier, gross_margin, free_cashflow, debt_to_assets,
               total_cur_assets, money_cap, invturn_days, arturn_days, end_date
        FROM fundamental_screen
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT {ph()}::int;
    """
    params.append(int(limit))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 财务数据表 financial_data (tushare 年报核心指标, 供财报分析)
# ---------------------------------------------------------------------------

FINANCIAL_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS financial_data (
    id                    BIGSERIAL PRIMARY KEY,
    ts_code               VARCHAR(16)  NOT NULL,
    name                  VARCHAR(64)  DEFAULT '',
    year                  INTEGER      NOT NULL,
    end_date              VARCHAR(16)  DEFAULT '',
    -- 比率 (tushare fina_indicator 原值)
    roe                   DOUBLE PRECISION,   -- ROE %
    net_margin            DOUBLE PRECISION,   -- 净利润率 %
    gross_margin          DOUBLE PRECISION,   -- 毛利率 %
    debt_to_assets        DOUBLE PRECISION,   -- 资产负债率 %
    current_ratio         DOUBLE PRECISION,   -- 流动比率
    quick_ratio           DOUBLE PRECISION,   -- 速动比率
    assets_turn           DOUBLE PRECISION,   -- 总资产周转率
    equity_multiplier     DOUBLE PRECISION,   -- 权益乘数
    ar_turn               DOUBLE PRECISION,   -- 应收账款周转率
    inv_turn              DOUBLE PRECISION,   -- 存货周转率
    or_yoy                DOUBLE PRECISION,   -- 营收同比 %
    netprofit_yoy         DOUBLE PRECISION,   -- 净利同比 %
    cash_ratio            DOUBLE PRECISION,   -- 净现比 = 经营现金流/净利润
    -- 金额 (亿元, 由元 /1e8)
    total_revenue         DOUBLE PRECISION,
    operate_cost          DOUBLE PRECISION,
    n_income              DOUBLE PRECISION,
    sell_exp              DOUBLE PRECISION,
    admin_exp             DOUBLE PRECISION,
    fin_exp               DOUBLE PRECISION,
    rd_exp                DOUBLE PRECISION,
    total_assets          DOUBLE PRECISION,
    total_cur_assets      DOUBLE PRECISION,
    money_cap             DOUBLE PRECISION,
    accounts_receiv       DOUBLE PRECISION,
    inventory             DOUBLE PRECISION,
    fixed_assets          DOUBLE PRECISION,
    contract_liab         DOUBLE PRECISION,
    total_liab            DOUBLE PRECISION,
    total_cur_liab        DOUBLE PRECISION,
    ocf                   DOUBLE PRECISION,   -- 经营现金流净额 (亿)
    icf                   DOUBLE PRECISION,   -- 投资现金流净额 (亿)
    fncf                  DOUBLE PRECISION,   -- 筹资现金流净额 (亿)
    -- 估值 (最新快照)
    close                 DOUBLE PRECISION,
    pe_ttm                DOUBLE PRECISION,
    pb                    DOUBLE PRECISION,
    dv_ratio              DOUBLE PRECISION,
    total_mv              DOUBLE PRECISION,
    updated_at            TIMESTAMP    DEFAULT now(),
    UNIQUE (ts_code, year)
);
CREATE INDEX IF NOT EXISTS idx_fin_data_code_year ON financial_data (ts_code, year);
"""

FINANCIAL_COLS = [
    "ts_code", "name", "year", "end_date",
    "roe", "net_margin", "gross_margin", "debt_to_assets",
    "current_ratio", "quick_ratio", "assets_turn", "equity_multiplier",
    "ar_turn", "inv_turn", "or_yoy", "netprofit_yoy", "cash_ratio",
    "total_revenue", "operate_cost", "n_income", "sell_exp", "admin_exp",
    "fin_exp", "rd_exp", "total_assets", "total_cur_assets", "money_cap",
    "accounts_receiv", "inventory", "fixed_assets", "contract_liab",
    "total_liab", "total_cur_liab", "ocf", "icf", "fncf",
    "close", "pe_ttm", "pb", "dv_ratio", "total_mv",
]

_FIN_UPSERT_SQL = _upsert_sql("financial_data", FINANCIAL_COLS, ("ts_code", "year"))


async def init_financial_schema() -> None:
    """创建 financial_data 表与索引 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(FINANCIAL_SCHEMA_DDL)


async def upsert_financial_rows(rows: list[dict]) -> int:
    """按 (ts_code, year) upsert 写入财务数据, 返回行数。"""
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_FIN_UPSERT_SQL, [tuple(r.get(c) for c in FINANCIAL_COLS) for r in rows])
    return len(rows)


async def has_financial(ts_code: str, year: int) -> bool:
    """该股票该年份是否已有财务数据。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 FROM financial_data WHERE ts_code=$1 AND year=$2 LIMIT 1;",
            ts_code, int(year)) is not None


async def query_financial_by_code(ts_code: str, years: list[int]) -> list[dict]:
    """查询某股票多年财务数据 (年报)。"""
    if not years:
        return []
    cols = ", ".join(FINANCIAL_COLS)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {cols} FROM financial_data "
            "WHERE ts_code=$1 AND year = ANY($2::int[]) ORDER BY year;",
            ts_code, [int(y) for y in years])
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 每日区间交易推荐表 daily_band_recommend (全市场每日参数估算结果)
# ---------------------------------------------------------------------------

DAILY_REC_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS daily_band_recommend (
    id             BIGSERIAL PRIMARY KEY,
    calc_date      VARCHAR(16)  NOT NULL,    -- 计算/收盘日 YYYYMMDD
    ts_code        VARCHAR(16)  NOT NULL,
    name           VARCHAR(64)  DEFAULT '',
    kind           VARCHAR(8)   DEFAULT 'stock',
    close          DOUBLE PRECISION,        -- 当日收盘价 (前复权)
    buy_price      DOUBLE PRECISION,
    sell_price     DOUBLE PRECISION,
    stop_price     DOUBLE PRECISION,
    total_return   DOUBLE PRECISION,        -- 总收益率 %
    annual_return  DOUBLE PRECISION,
    max_drawdown   DOUBLE PRECISION,
    sharpe         DOUBLE PRECISION,
    calmar         DOUBLE PRECISION,
    trades         INTEGER,
    objective      VARCHAR(16)  DEFAULT 'balanced',
    industry       VARCHAR(64)  DEFAULT '',   -- 东财行业 (如 白酒/银行), 用于按行业隔离
    achieved       BOOLEAN      DEFAULT FALSE,  -- 夏普是否达标
    updated_at     TIMESTAMP    DEFAULT now(),
    UNIQUE (calc_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_dbr_date ON daily_band_recommend (calc_date);
"""

DAILY_REC_COLS = [
    "calc_date", "ts_code", "name", "kind", "close",
    "buy_price", "sell_price", "stop_price",
    "total_return", "annual_return", "max_drawdown", "sharpe", "calmar",
    "trades", "objective", "industry", "achieved",
]

_DR_UPSERT_SQL = _upsert_sql("daily_band_recommend", DAILY_REC_COLS, ("calc_date", "ts_code"))


async def init_daily_rec_schema() -> None:
    """创建 daily_band_recommend 表与索引 (幂等), 旧表自动补 industry 列。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DAILY_REC_SCHEMA_DDL)
        # 旧表迁移: 补 industry 列
        has_col = await conn.fetchval(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'daily_band_recommend' AND column_name = 'industry'")
        if has_col is None:
            await conn.execute("ALTER TABLE daily_band_recommend ADD COLUMN industry VARCHAR(64) DEFAULT ''")


async def upsert_daily_rec_rows(rows: list[dict]) -> int:
    """按 (calc_date, ts_code) upsert 写入每日推荐, 返回行数。"""
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_DR_UPSERT_SQL, [tuple(r.get(c) for c in DAILY_REC_COLS) for r in rows])
    return len(rows)


async def has_daily_rec(calc_date: str, industry: str = "") -> int:
    """统计某计算日 (可选行业) 已入库的每日推荐行数; 用于缓存命中判断。"""
    if not calc_date:
        return 0
    sql = "SELECT COUNT(*) FROM daily_band_recommend WHERE calc_date = $1"
    params: list = [calc_date]
    if industry.strip():
        sql += " AND industry LIKE $2"
        params.append(f"%{industry.strip()}%")
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(sql, *params) or 0)


async def daily_rec_done_codes(calc_date: str) -> list:
    """某计算日已入库的 ts_code 列表 (用于全市场续跑跳过)。"""
    if not calc_date:
        return []
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT ts_code FROM daily_band_recommend WHERE calc_date = $1", calc_date)
    return [str(r[0]) for r in rows]


async def backfill_daily_rec_industry(ts_code_industry: dict) -> int:
    """回填 daily_band_recommend 中 industry 为空的行 (按 ts_code 映射), 返回更新行数。"""
    if not ts_code_industry:
        return 0
    n = 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("SELECT id, ts_code FROM daily_band_recommend "
                                    "WHERE industry IS NULL OR industry = ''")
            for r in rows:
                ind = ts_code_industry.get(str(r["ts_code"]), "")
                if ind:
                    await conn.execute("UPDATE daily_band_recommend SET industry=$1 WHERE id=$2",
                                       ind, r["id"])
                    n += 1
    return n


async def latest_calc_date() -> str:
    """最近一次计算的 calc_date。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT max(calc_date) FROM daily_band_recommend;")
    return str(v or "")


async def query_daily_recommend(calc_date: str | None = None, buy_above_close: bool = True,
                                limit: int = 500, industry: str = "") -> list[dict]:
    """查询某计算日的推荐 (buy_price >= close), 按 close 降序; industry 非空时按行业子串过滤。"""
    if not calc_date:
        calc_date = await latest_calc_date()
    if not calc_date:
        return []
    sql = "SELECT * FROM daily_band_recommend WHERE calc_date = $1"
    params: list = [calc_date]
    if buy_above_close:
        sql += " AND buy_price >= close"
    if industry.strip():
        sql += " AND industry LIKE $2"
        params.append(f"%{industry.strip()}%")
    sql += f" ORDER BY close DESC LIMIT ${len(params) + 1}::int"
    params.append(int(limit))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def latest_rlv_year() -> int:
    """red_low_vol 最新数据年份。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT max(year) FROM red_low_vol;")
    return int(v or 0)


async def query_dividend_recommend(min_dy_ttm: float = 3.0, industry: str = "",
                                   year_min: int | None = None, year_max: int | None = None,
                                   limit: int = 500,
                                   payout_min: float | None = None,
                                   payout_max: float | None = None,
                                   roe_min: float | None = None,
                                   roe_max: float | None = None) -> list[dict]:
    """红利低波动态股息率推荐: 年份区间 股息率-TTM >= N 的公司, 按 ttm 降序。

    直接读 red_low_vol (无需重新计算), 供每日推荐 方法2 使用。
    year_min/year_max: 年份区间 (均空→最新单年; 只填一个→单边); payout/roe 为可选范围。
    """
    year_conds: list[str] = []
    year_params: list = []
    if year_min:
        year_conds.append("year >= $%d" % (len(year_params) + 1))
        year_params.append(int(year_min))
    if year_max:
        year_conds.append("year <= $%d" % (len(year_params) + 1))
        year_params.append(int(year_max))
    if not year_conds:
        latest = await latest_rlv_year()
        if not latest:
            return []
        year_conds.append("year = $1")
        year_params.append(latest)
    sql = ("SELECT ts_code, symbol, name, industry, year, "
           "dividend_yield, dividend_yield_ttm, last_close, volatility, div_per_share, "
           "payout_ratio, dividend_growth_3y, roe, debt_to_assets "
           "FROM red_low_vol WHERE " + " AND ".join(year_conds))
    params: list = year_params
    if industry.strip():
        sql += " AND industry LIKE $%d" % (len(params) + 1)
        params.append(f"%{industry.strip()}%")
    if min_dy_ttm is not None and float(min_dy_ttm) > 0:
        sql += " AND dividend_yield_ttm >= $%d" % (len(params) + 1)
        params.append(float(min_dy_ttm))
    if payout_min is not None:
        sql += " AND payout_ratio >= $%d" % (len(params) + 1)
        params.append(float(payout_min))
    if payout_max is not None:
        sql += " AND payout_ratio <= $%d" % (len(params) + 1)
        params.append(float(payout_max))
    if roe_min is not None:
        sql += " AND roe >= $%d" % (len(params) + 1)
        params.append(float(roe_min))
    if roe_max is not None:
        sql += " AND roe <= $%d" % (len(params) + 1)
        params.append(float(roe_max))
    sql += " ORDER BY dividend_yield_ttm DESC NULLS LAST, ts_code ASC LIMIT $%d::int" % (len(params) + 1)
    params.append(int(limit))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 我的股票表 my_stocks (自选股, 从股票详情页添加)
# ---------------------------------------------------------------------------

MY_STOCKS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS my_stocks (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT       NOT NULL DEFAULT 0,   -- 归属用户 (0=旧数据/未归属, 不对外展示)
    ts_code     VARCHAR(16)  NOT NULL,
    name        VARCHAR(64)  NOT NULL DEFAULT '',
    added_at    TIMESTAMP    DEFAULT now(),
    UNIQUE (user_id, ts_code)
);
"""


async def init_my_stocks_schema() -> None:
    """创建 my_stocks 表 (幂等), 并为旧表迁移 user_id 列与唯一约束。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(MY_STOCKS_SCHEMA_DDL)
            # 旧表迁移: 新增 user_id (旧数据归 0 隐藏), 唯一约束从 ts_code 改为 (user_id, ts_code)
            await conn.execute("ALTER TABLE my_stocks ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 0;")
            await conn.execute("ALTER TABLE my_stocks DROP CONSTRAINT IF EXISTS my_stocks_ts_code_key;")
            await conn.execute("DROP INDEX IF EXISTS my_stocks_ts_code_key;")
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_my_stocks_user_ts ON my_stocks (user_id, ts_code);")


async def add_my_stock(user_id: int, ts_code: str, name: str) -> bool:
    """为指定用户添加自选股 ((user_id, ts_code) 唯一), 返回是否为新插入。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO my_stocks (user_id, ts_code, name) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, ts_code) DO NOTHING RETURNING id",
            user_id, ts_code, name)
    return row is not None


async def remove_my_stock(user_id: int, ts_code: str) -> int:
    """移除指定用户的自选股, 返回删除行数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("DELETE FROM my_stocks WHERE user_id = $1 AND ts_code = $2 RETURNING id",
                                user_id, ts_code)
    return len(rows)


async def list_my_stocks(user_id: int) -> list[dict]:
    """列出指定用户的全部自选股 (按添加时间升序)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts_code, name, added_at FROM my_stocks "
            "WHERE user_id = $1 ORDER BY added_at ASC, id ASC", user_id)
    return [dict(r) for r in rows]


async def has_my_stock(user_id: int, ts_code: str) -> bool:
    """某股票是否已在指定用户的自选股中。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 FROM my_stocks WHERE user_id = $1 AND ts_code = $2 LIMIT 1",
            user_id, ts_code) is not None


# ---------------------------------------------------------------------------
# 用户认证: users + sessions 表
# ---------------------------------------------------------------------------

AUTH_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      VARCHAR(32)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMP    DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
    token       VARCHAR(64) PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    created_at  TIMESTAMP DEFAULT now(),
    expires_at  TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
"""


async def init_auth_schema() -> None:
    """创建 users / sessions 表 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(AUTH_SCHEMA_DDL)


async def create_user(username: str, password_hash: str) -> tuple[int | None, bool]:
    """创建用户, 返回 (user_id, 是否新插入)。用户名已存在时 user_id 为 None。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (username, password_hash) VALUES ($1, $2) "
            "ON CONFLICT (username) DO NOTHING RETURNING id",
            username, password_hash)
    return (int(row["id"]) if row else None, row is not None)


async def get_user_by_username(username: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = $1",
            username)
    return dict(r) if r else None


async def get_user_by_id(user_id: int) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT id, username, created_at FROM users WHERE id = $1", user_id)
    return dict(r) if r else None


async def create_session(token: str, user_id: int, expires_at) -> None:
    # asyncpg 对 timestamp (无时区) 列要求 naive datetime; tz-aware (如 main 传入的
    # datetime.now(timezone.utc)) 需转成 UTC naive, 否则 asyncpg 报错
    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES ($1, $2, $3)",
                           token, user_id, expires_at)


async def get_session_user(token: str) -> dict | None:
    """按会话 token 返回 {id, username} (已过期返回 None)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = $1 AND s.expires_at > now()", token)
    return dict(r) if r else None


async def delete_session(token: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE token = $1", token)


async def delete_user(user_id: int) -> None:
    """注销账号: 删除该用户的会话、自选股及用户记录 (不可恢复)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM my_stocks WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM sessions WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)


# ---------------------------------------------------------------------------
# 港股红利低波表 hk_red_low_vol (数据源: 东财港股财务/分红 + 腾讯日线 + tushare hk_basic)
# ---------------------------------------------------------------------------

HK_RLV_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS hk_red_low_vol (
    id                    BIGSERIAL PRIMARY KEY,
    ts_code               VARCHAR(16)  NOT NULL,
    symbol                VARCHAR(8)   NOT NULL,
    name                  VARCHAR(64)  NOT NULL,
    industry              VARCHAR(64)  DEFAULT '',
    market                VARCHAR(16)  DEFAULT '',
    year                  INTEGER      NOT NULL,
    dividend_yield        DOUBLE PRECISION,   -- 静态股息率 % (当年每股分红 / 年末收盘价)
    dividend_yield_ttm    DOUBLE PRECISION,   -- 股息率-TTM % (最新财政年度每股分红 / 最新收盘价)
    last_close            DOUBLE PRECISION,   -- 最新收盘价 (港元)
    volatility            DOUBLE PRECISION,   -- 年化波动率 % (日收益 std * sqrt(252))
    div_per_share         DOUBLE PRECISION,   -- 每股现金分红 (港元)
    free_cashflow         DOUBLE PRECISION,   -- 企业自由现金流 ≈ OCF+ICF (万港元)
    gross_margin          DOUBLE PRECISION,   -- 毛利率 %
    eps                   DOUBLE PRECISION,   -- 每股收益 (港元)
    payout_ratio          DOUBLE PRECISION,   -- 分红率 % (每股分红/每股收益)
    dividend_growth_3y    DOUBLE PRECISION,   -- 3 年每股股利复合增长率 %
    roe                   DOUBLE PRECISION,   -- 净资产收益率 %
    debt_to_assets        DOUBLE PRECISION,   -- 资产负债率 %
    avg_daily_mv          DOUBLE PRECISION,   -- 总市值 (万港元, 年报口径)
    avg_daily_amt         DOUBLE PRECISION,   -- 日均成交金额 (万港元, 近似=成交量×收盘价)
    end_date              VARCHAR(16)  DEFAULT '',
    updated_at            TIMESTAMP    DEFAULT now(),
    UNIQUE (ts_code, year)
);
CREATE INDEX IF NOT EXISTS idx_hk_rlv_ind_year ON hk_red_low_vol (industry, year);
"""

HK_RLV_SORTABLE_COLUMNS = {
    "year": "year",
    "name": "name",
    "dividend_yield": "dividend_yield",
    "dividend_yield_ttm": "dividend_yield_ttm",
    "volatility": "volatility",
    "div_per_share": "div_per_share",
    "free_cashflow": "free_cashflow",
    "gross_margin": "gross_margin",
    "eps": "eps",
    "payout_ratio": "payout_ratio",
    "roe": "roe",
    "debt_to_assets": "debt_to_assets",
    "avg_daily_mv": "avg_daily_mv",
    "avg_daily_amt": "avg_daily_amt",
    "dividend_growth_3y": "dividend_growth_3y",
    "last_close": "last_close",
}

_HK_RLV_COLS = [
    "ts_code", "symbol", "name", "industry", "market", "year",
    "dividend_yield", "dividend_yield_ttm", "last_close", "volatility", "div_per_share", "free_cashflow", "gross_margin",
    "eps", "payout_ratio", "dividend_growth_3y", "roe", "debt_to_assets",
    "avg_daily_mv", "avg_daily_amt", "end_date",
]

_HK_RLV_UPSERT_SQL = _upsert_sql("hk_red_low_vol", _HK_RLV_COLS, ("ts_code", "year"))


async def init_hk_rlv_schema() -> None:
    """创建 hk_red_low_vol 表与索引 (幂等), 并对旧表迁移新增列。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(HK_RLV_SCHEMA_DDL)
        # 旧表迁移: 新增 毛利率 列
        await conn.execute("ALTER TABLE hk_red_low_vol ADD COLUMN IF NOT EXISTS gross_margin DOUBLE PRECISION;")


async def upsert_hk_rlv_rows(rows: list[dict]) -> int:
    """按 (ts_code, year) upsert 写入港股红利低波, 返回行数。"""
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_HK_RLV_UPSERT_SQL, [tuple(r.get(c) for c in _HK_RLV_COLS) for r in rows])
    return len(rows)


async def count_hk_rlv_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM hk_red_low_vol WHERE industry LIKE $1 AND year = $2;",
            f"%{industry}%", year))


async def query_hk_rlv(industry: str, years: list[int], sort_by: str = "dividend_yield",
                       order: str = "desc", limit: int = 500,
                       filters: dict | None = None) -> list[dict]:
    """港股红利低波: 按行业+多年份查询, 支持阈值筛选与排序 (字段白名单防注入)。"""
    col = HK_RLV_SORTABLE_COLUMNS.get(sort_by, "dividend_yield")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []

    def ph() -> str:
        return f"${len(params) + 1}"

    if industry:
        conds.append(f"industry LIKE {ph()}")
        params.append(f"%{industry}%")
    if years:
        conds.append(f"year = ANY({ph()}::int[])")
        params.append([int(y) for y in years])
    for key, flt in (filters or {}).items():
        col_name = HK_RLV_SORTABLE_COLUMNS.get(key)
        if col_name is None:
            continue
        if isinstance(flt, dict):
            mn, mx = flt.get("min"), flt.get("max")
        else:
            mn, mx = flt, None
        if mn is not None:
            conds.append(f"{col_name} >= {ph()}")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= {ph()}")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, market, year,
               dividend_yield, dividend_yield_ttm, last_close, volatility, div_per_share, free_cashflow, gross_margin,
               eps, payout_ratio, dividend_growth_3y, roe, debt_to_assets,
               avg_daily_mv, avg_daily_amt, end_date
        FROM hk_red_low_vol
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT {ph()}::int;
    """
    params.append(int(limit))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 港股基本面表 hk_fundamental_screen (ROE 杜邦拆分等)
# ---------------------------------------------------------------------------

HK_FUNDAMENTAL_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS hk_fundamental_screen (
    id                    BIGSERIAL PRIMARY KEY,
    ts_code               VARCHAR(16)  NOT NULL,
    symbol                VARCHAR(8)   NOT NULL,
    name                  VARCHAR(64)  NOT NULL,
    industry              VARCHAR(64)  DEFAULT '',
    market                VARCHAR(16)  DEFAULT '',
    year                  INTEGER      NOT NULL,
    close                 DOUBLE PRECISION,   -- 年末收盘价 (港元)
    roe                   DOUBLE PRECISION,   -- ROE %
    net_margin            DOUBLE PRECISION,   -- 净利润率 %
    assets_turn           DOUBLE PRECISION,   -- 总资产周转率 = 营收/总资产
    equity_multiplier     DOUBLE PRECISION,   -- 权益乘数 = 总资产/归母权益
    gross_margin          DOUBLE PRECISION,   -- 毛利率 %
    free_cashflow         DOUBLE PRECISION,   -- 企业自由现金流 ≈ OCF+ICF (万港元)
    debt_to_assets        DOUBLE PRECISION,   -- 资产负债率 %
    current_ratio         DOUBLE PRECISION,   -- 流动比率
    total_cur_assets      DOUBLE PRECISION,   -- 流动资产 (万港元)
    money_cap             DOUBLE PRECISION,   -- 现金及等价物 (万港元)
    invturn_days          DOUBLE PRECISION,   -- 存货周转天数
    arturn_days           DOUBLE PRECISION,   -- 应收账款周转天数
    eps                   DOUBLE PRECISION,   -- 每股收益 (港元)
    operate_income        DOUBLE PRECISION,   -- 营业收入 (万港元)
    net_profit            DOUBLE PRECISION,   -- 净利润 (万港元)
    total_mv              DOUBLE PRECISION,   -- 总市值 (万港元)
    end_date              VARCHAR(16)  DEFAULT '',
    updated_at            TIMESTAMP    DEFAULT now(),
    UNIQUE (ts_code, year)
);
CREATE INDEX IF NOT EXISTS idx_hk_fs_ind_year ON hk_fundamental_screen (industry, year);
"""

HK_FUNDAMENTAL_SORTABLE_COLUMNS = {
    "year": "year",
    "name": "name",
    "close": "close",
    "roe": "roe",
    "net_margin": "net_margin",
    "assets_turn": "assets_turn",
    "equity_multiplier": "equity_multiplier",
    "gross_margin": "gross_margin",
    "free_cashflow": "free_cashflow",
    "debt_to_assets": "debt_to_assets",
    "current_ratio": "current_ratio",
    "total_cur_assets": "total_cur_assets",
    "money_cap": "money_cap",
    "invturn_days": "invturn_days",
    "arturn_days": "arturn_days",
    "eps": "eps",
    "operate_income": "operate_income",
    "net_profit": "net_profit",
    "total_mv": "total_mv",
}

_HK_FUND_COLS = [
    "ts_code", "symbol", "name", "industry", "market", "year",
    "close", "roe", "net_margin", "assets_turn", "equity_multiplier",
    "gross_margin", "free_cashflow", "debt_to_assets", "current_ratio", "total_cur_assets", "money_cap",
    "invturn_days", "arturn_days", "eps", "operate_income", "net_profit", "total_mv", "end_date",
]

_HK_FUND_UPSERT_SQL = _upsert_sql("hk_fundamental_screen", _HK_FUND_COLS, ("ts_code", "year"))


# Alpha158 回测: 日线表 stock_daily_bars (数据保障 + qlib 构建数据源)
ALPHA158_SCHEMA_DDL = """
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


async def init_alpha158_schema() -> None:
    """创建 stock_daily_bars 表与索引 (幂等), 供 Alpha158 回测数据保障/qlib 构建。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(ALPHA158_SCHEMA_DDL)
        # 迁移: 新增 kind 列 (stock 股票 / fund ETF), 供本地日线持久化区分数据源
        await conn.execute("ALTER TABLE stock_daily_bars ADD COLUMN IF NOT EXISTS kind VARCHAR(8) DEFAULT 'stock';")
        # 迁移: 新增 updated_at (upsert 通用 SQL 需要该列)
        await conn.execute("ALTER TABLE stock_daily_bars ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();")


_DAILY_BARS_COLS = [
    "symbol", "kind", "trade_date", "open", "high", "low", "close",
    "pre_close", "pct_chg", "vol", "amount", "vwap", "adj_factor", "turnover_rate",
]
_DAILY_BARS_UPSERT_SQL = _upsert_sql("stock_daily_bars", _DAILY_BARS_COLS, ("symbol", "trade_date"))


# ---------------------------------------------------------------------------
# 本地估值/换手率表 stock_daily_basic (daily_basic 持久化, 详情页 PB/PE/市值/换手率)
# ---------------------------------------------------------------------------

DAILY_BASIC_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS stock_daily_basic (
    id            BIGSERIAL PRIMARY KEY,
    symbol        VARCHAR(16)  NOT NULL,
    trade_date    DATE         NOT NULL,
    close         NUMERIC(14,4),
    pb            NUMERIC(14,4),
    pe            NUMERIC(14,4),
    pe_ttm        NUMERIC(14,4),
    total_share   NUMERIC(20,4),   -- 总股本(万股)
    float_share   NUMERIC(20,4),   -- 流通股本(万股)
    total_mv      NUMERIC(24,4),   -- 总市值(万元)
    circ_mv       NUMERIC(24,4),   -- 流通市值(万元)
    dv_ratio      NUMERIC(10,4),   -- 股息率 %
    dv_ttm        NUMERIC(10,4),   -- 股息率-TTM %
    turnover_rate NUMERIC(10,4),   -- 换手率 %
    updated_at    TIMESTAMP DEFAULT now(),
    UNIQUE (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_sdb_symbol_date ON stock_daily_basic (symbol, trade_date);
"""

DAILY_BASIC_COLS = [
    "symbol", "trade_date", "close", "pb", "pe", "pe_ttm", "total_share",
    "float_share", "total_mv", "circ_mv", "dv_ratio", "dv_ttm", "turnover_rate",
]
_DAILY_BASIC_UPSERT_SQL = _upsert_sql("stock_daily_basic", DAILY_BASIC_COLS, ("symbol", "trade_date"))


async def init_daily_basic_schema() -> None:
    """创建 stock_daily_basic 表与索引 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DAILY_BASIC_SCHEMA_DDL)


async def upsert_daily_basic_rows(rows: list[tuple]) -> int:
    """按 (symbol, trade_date) upsert 写入估值/换手率, 返回行数。"""
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_DAILY_BASIC_UPSERT_SQL, rows)
    return len(rows)


async def daily_basic_stats(symbol: str) -> dict | None:
    """查询某 symbol 在 stock_daily_basic 的覆盖统计 {n, min_date, max_date}。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT count(*) n, min(trade_date) mn, max(trade_date) mx "
            "FROM stock_daily_basic WHERE symbol = $1", symbol)
    if not r or not r["n"]:
        return None
    return {"n": int(r["n"]),
            "min_date": str(r["mn"]),
            "max_date": str(r["mx"])}


async def latest_daily_bars_batch(symbols: list[str]) -> list[dict]:
    """批量查询每组 symbol 最新交易日的日线 (DISTINCT ON), 供自选股快照从本地 pg 读最新行情。

    返回 [{symbol, trade_date, close, pre_close, pct_chg}]。
    """
    if not symbols:
        return []
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (symbol) symbol, trade_date, close, pre_close, pct_chg "
            "FROM stock_daily_bars WHERE symbol = ANY($1::text[]) "
            "ORDER BY symbol, trade_date DESC", list(symbols))
    return [dict(r) for r in rows]


async def latest_daily_basic_batch(symbols: list[str]) -> list[dict]:
    """批量查询每组 symbol 最新交易日的估值/换手率 (DISTINCT ON), 供自选股快照从本地 pg 读估值。

    返回 [{symbol, trade_date, pb, pe, pe_ttm, total_share, float_share,
          total_mv, circ_mv, dv_ratio, dv_ttm, turnover_rate}]。
    """
    if not symbols:
        return []
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (symbol) symbol, trade_date, pb, pe, pe_ttm, total_share, "
            "float_share, total_mv, circ_mv, dv_ratio, dv_ttm, turnover_rate "
            "FROM stock_daily_basic WHERE symbol = ANY($1::text[]) "
            "ORDER BY symbol, trade_date DESC", list(symbols))
    return [dict(r) for r in rows]


async def query_daily_basic(symbol: str, start_date: str = "", end_date: str = "") -> list[dict]:
    """查询某 symbol 估值/换手率 (升序)。start/end 支持 YYYYMMDD 或 YYYY-MM-DD。"""

    def _d(s: str):
        s = s.strip().replace("-", "")
        return datetime.strptime(s[:8], "%Y%m%d").date() if len(s) >= 8 else None

    sql = "SELECT trade_date, close, pb, pe, pe_ttm, total_share, float_share, " \
          "total_mv, circ_mv, dv_ratio, dv_ttm, turnover_rate " \
          "FROM stock_daily_basic WHERE symbol = $1"
    params: list = [symbol]
    if start_date and _d(start_date):
        sql += f" AND trade_date >= ${len(params) + 1}::date"
        params.append(_d(start_date))
    if end_date and _d(end_date):
        sql += f" AND trade_date <= ${len(params) + 1}::date"
        params.append(_d(end_date))
    sql += " ORDER BY trade_date ASC"
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 本地分红明细表 stock_dividends (dividend 持久化, 详情页每股分红/股息率)
# ---------------------------------------------------------------------------

DIVIDEND_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS stock_dividends (
    id         BIGSERIAL PRIMARY KEY,
    symbol     VARCHAR(16)  NOT NULL,
    end_date   VARCHAR(16)  NOT NULL,     -- 分红年度/截止日 YYYYMMDD
    div_proc   VARCHAR(16)  DEFAULT '',   -- 实施/预案/股东大会通过...
    cash_div   NUMERIC(14,6),             -- 每股派息(元)
    stk_div    NUMERIC(14,6),             -- 每股送股(股)
    stk_bo_rate NUMERIC(14,6),            -- 每股转增(股)
    ann_date   VARCHAR(16)  DEFAULT '',   -- 公告日
    record_date VARCHAR(16) DEFAULT '',   -- 股权登记日
    ex_date    VARCHAR(16)  DEFAULT '',
    pay_date   VARCHAR(16)  DEFAULT '',
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE (symbol, end_date, div_proc)
);
CREATE INDEX IF NOT EXISTS idx_sd_symbol_date ON stock_dividends (symbol, end_date);
"""

DIVIDEND_COLS = ["symbol", "end_date", "div_proc", "cash_div", "stk_div",
                 "stk_bo_rate", "ann_date", "record_date", "ex_date", "pay_date"]
_DIVIDEND_UPSERT_SQL = _upsert_sql("stock_dividends", DIVIDEND_COLS, ("symbol", "end_date", "div_proc"))


async def init_dividend_schema() -> None:
    """创建 stock_dividends 表与索引 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DIVIDEND_SCHEMA_DDL)


async def upsert_dividend_rows(rows: list[tuple]) -> int:
    """按 (symbol, end_date, div_proc) upsert 写入分红明细, 返回行数。"""
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_DIVIDEND_UPSERT_SQL, rows)
    return len(rows)


async def query_dividends(symbol: str) -> list[dict]:
    """查询某 symbol 全部分红明细 (升序)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT end_date, div_proc, cash_div, stk_div, stk_bo_rate, "
            "ann_date, record_date, ex_date, pay_date "
            "FROM stock_dividends WHERE symbol = $1 ORDER BY end_date ASC, id ASC",
            symbol)
    return [dict(r) for r in rows]


async def has_dividends(symbol: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 FROM stock_dividends WHERE symbol = $1 LIMIT 1", symbol) is not None


async def upsert_daily_bars(rows: list[tuple]) -> int:
    """按 (symbol, trade_date) upsert 写入日线 (含复权因子/换手率), 返回行数。

    rows: [(symbol, kind, trade_date(date), open, high, low, close,
            pre_close, pct_chg, vol, amount, vwap, adj_factor, turnover_rate), ...]
    """
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_DAILY_BARS_UPSERT_SQL, rows)
    return len(rows)


async def daily_bars_stats(symbol: str) -> dict | None:
    """查询某 symbol 在 stock_daily_bars 的覆盖统计 {n, min_date, max_date}。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT count(*) n, min(trade_date) mn, max(trade_date) mx "
            "FROM stock_daily_bars WHERE symbol = $1", symbol)
    if not r or not r["n"]:
        return None
    return {"n": int(r["n"]),
            "min_date": str(r["mn"]),
            "max_date": str(r["mx"])}


async def query_daily_bars(symbol: str, start_date: str = "", end_date: str = "") -> list[dict]:
    """查询某 symbol 日线 (升序), 返回 dict 列表 (含 adj_factor/turnover_rate)。

    start_date/end_date 支持 YYYYMMDD 或 YYYY-MM-DD。
    """
    def _d(s: str):
        s = s.strip().replace("-", "")
        return datetime.strptime(s[:8], "%Y%m%d").date() if len(s) >= 8 else None

    sql = "SELECT symbol, kind, trade_date, open, high, low, close, pre_close, " \
          "pct_chg, vol, amount, vwap, adj_factor, turnover_rate " \
          "FROM stock_daily_bars WHERE symbol = $1"
    params: list = [symbol]
    if start_date and _d(start_date):
        sql += f" AND trade_date >= ${len(params) + 1}::date"
        params.append(_d(start_date))
    if end_date and _d(end_date):
        sql += f" AND trade_date <= ${len(params) + 1}::date"
        params.append(_d(end_date))
    sql += " ORDER BY trade_date ASC"
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def target_sync_codes() -> list[str]:
    """目标股票列表: 我的股票(全部用户) ∪ 策略Hub策略股票 ∪ ETF 的 ts_code 去重。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts_code FROM my_stocks "
            "UNION SELECT ts_code FROM custom_strategy_stocks "
            "UNION SELECT ts_code FROM etf_screen;")
    return [str(r["ts_code"]) for r in rows]


async def init_hk_fundamental_schema() -> None:
    """创建 hk_fundamental_screen 表与索引 (幂等), 并对旧表迁移新增列。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(HK_FUNDAMENTAL_SCHEMA_DDL)
        # 旧表迁移: 新增 自由现金流 列
        await conn.execute("ALTER TABLE hk_fundamental_screen ADD COLUMN IF NOT EXISTS free_cashflow DOUBLE PRECISION;")


async def upsert_hk_fundamental_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_HK_FUND_UPSERT_SQL, [tuple(r.get(c) for c in _HK_FUND_COLS) for r in rows])
    return len(rows)


async def count_hk_fundamental_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM hk_fundamental_screen WHERE industry LIKE $1 AND year = $2;",
            f"%{industry}%", year))


async def query_hk_fundamental(industry: str, years: list[int], sort_by: str = "roe",
                               order: str = "desc", limit: int = 1000,
                               filters: dict | None = None) -> list[dict]:
    """港股基本面: 按行业+多年份查询, 支持阈值筛选与排序。"""
    col = HK_FUNDAMENTAL_SORTABLE_COLUMNS.get(sort_by, "roe")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []

    def ph() -> str:
        return f"${len(params) + 1}"

    if industry:
        conds.append(f"industry LIKE {ph()}")
        params.append(f"%{industry}%")
    if years:
        conds.append(f"year = ANY({ph()}::int[])")
        params.append([int(y) for y in years])
    for key, flt in (filters or {}).items():
        col_name = HK_FUNDAMENTAL_SORTABLE_COLUMNS.get(key)
        if col_name is None:
            continue
        if isinstance(flt, dict):
            mn, mx = flt.get("min"), flt.get("max")
        else:
            mn, mx = flt, None
        if mn is not None:
            conds.append(f"{col_name} >= {ph()}")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= {ph()}")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, market, year, close, roe, net_margin,
               assets_turn, equity_multiplier, gross_margin, free_cashflow, debt_to_assets, current_ratio,
               total_cur_assets, money_cap, invturn_days, arturn_days,
               eps, operate_income, net_profit, total_mv, end_date
        FROM hk_fundamental_screen
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT {ph()}::int;
    """
    params.append(int(limit))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def hk_synced_ts_codes(years: list[int]) -> set:
    """港股两表在给定年份全部有数据的 ts_code 集合 (全市场续跑断点用)。

    某股票被视为「已同步」= hk_red_low_vol 与 hk_fundamental_screen 中,
    给定年份全部存在 (count(DISTINCT year) = len(years))。
    """
    years = [int(y) for y in years]
    if not years:
        return set()
    result: set | None = None
    pool = await _get_pool()
    async with pool.acquire() as conn:
        for table in ("hk_red_low_vol", "hk_fundamental_screen"):
            rows = await conn.fetch(
                f"SELECT ts_code FROM {table} WHERE year = ANY($1::int[]) "
                "GROUP BY ts_code HAVING count(DISTINCT year) = $2",
                years, len(years))
            s = {str(r[0]) for r in rows}
            result = s if result is None else (result & s)
    return result or set()


# ---------------------------------------------------------------------------
# ETF 筛选表 etf_screen (夜间初始化脚本 scripts/init_etf.py 写入, 前端读库)
# ---------------------------------------------------------------------------

ETF_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS etf_screen (
    ts_code        VARCHAR(16) PRIMARY KEY,
    name           VARCHAR(100) DEFAULT '',
    fund_type      VARCHAR(20)  DEFAULT '',
    management     VARCHAR(100) DEFAULT '',
    found_date     VARCHAR(8)   DEFAULT '',
    list_date      VARCHAR(8)   DEFAULT '',
    age_years      DOUBLE PRECISION,   -- 存续年限 (上市日至今)
    close          DOUBLE PRECISION,   -- 最新收盘价
    pct_chg        DOUBLE PRECISION,   -- 当日涨跌幅 %
    unit_nav       DOUBLE PRECISION,   -- 最新单位净值
    fd_share       DOUBLE PRECISION,   -- 最新份额 (亿份)
    scale          DOUBLE PRECISION,   -- 基金规模 (亿元 = 份额×净值)
    m_fee          DOUBLE PRECISION,   -- 管理费率 %
    c_fee          DOUBLE PRECISION,   -- 托管费率 %
    total_fee      DOUBLE PRECISION,   -- 合计费率 %
    avg_amount_20  DOUBLE PRECISION,   -- 近20日均成交额 (万元)
    avg_amount_5   DOUBLE PRECISION,   -- 近5日均成交额 (万元)
    avg_vol_20     DOUBLE PRECISION,   -- 近20日均成交量 (手)
    premium        DOUBLE PRECISION,   -- 折溢价 % (收盘-净值)/净值×100
    track_dev      DOUBLE PRECISION,   -- 近20日日均跟踪偏离度 %
    high52         DOUBLE PRECISION,   -- 52周最高
    low52          DOUBLE PRECISION,   -- 52周最低
    pos52          DOUBLE PRECISION,   -- 52周位置 (0~1)
    calc_date      VARCHAR(8)  DEFAULT '',   -- 计算日 (最新交易日)
    updated_at     TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_etf_type  ON etf_screen (fund_type);
CREATE INDEX IF NOT EXISTS idx_etf_scale ON etf_screen (scale);
CREATE INDEX IF NOT EXISTS idx_etf_calc  ON etf_screen (calc_date);
"""

ETF_SORTABLE_COLUMNS = {
    "ts_code": "ts_code",
    "name": "name",
    "fund_type": "fund_type",
    "close": "close",
    "pct_chg": "pct_chg",
    "scale": "scale",
    "fd_share": "fd_share",
    "m_fee": "m_fee",
    "c_fee": "c_fee",
    "total_fee": "total_fee",
    "avg_amount_20": "avg_amount_20",
    "premium": "premium",
    "track_dev": "track_dev",
    "pos52": "pos52",
    "high52": "high52",
    "low52": "low52",
    "age_years": "age_years",
    "list_date": "list_date",
}

_ETF_COLS = [
    "ts_code", "name", "fund_type", "management", "found_date", "list_date",
    "age_years", "close", "pct_chg", "unit_nav", "fd_share", "scale",
    "m_fee", "c_fee", "total_fee", "avg_amount_20", "avg_amount_5", "avg_vol_20",
    "premium", "track_dev", "high52", "low52", "pos52", "calc_date",
]

_ETF_UPSERT_SQL = _upsert_sql("etf_screen", _ETF_COLS, ("ts_code",))


async def init_etf_schema() -> None:
    """创建 etf_screen 表与索引 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(ETF_SCHEMA_DDL)


async def upsert_etf_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(_ETF_UPSERT_SQL, [tuple(r.get(c) for c in _ETF_COLS) for r in rows])
    return len(rows)


async def latest_etf_calc_date() -> str:
    """etf_screen 中最近计算日 (空串=尚无数据)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT max(calc_date) FROM etf_screen;")
    return str(v) if v else ""


async def count_etf_by_calc_date(calc_date: str) -> int:
    """指定计算日的 ETF 记录数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM etf_screen WHERE calc_date = $1;",
                                       calc_date))


async def query_etf(calc_date: str = "", keyword: str = "", fund_type: str = "",
                    min_scale: float | None = None, max_m_fee: float | None = None,
                    max_c_fee: float | None = None, min_amount_20: float | None = None,
                    max_premium: float | None = None, min_pos52: float | None = None,
                    max_pos52: float | None = None,
                    sort_by: str = "scale", order: str = "desc",
                    limit: int = 300) -> list[dict]:
    """查询 ETF 筛选数据, 支持阈值筛选与排序 (NULL 值排最后)。"""
    col = ETF_SORTABLE_COLUMNS.get(sort_by, "scale")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []
    _ph_n = 0

    def ph() -> str:
        """递增占位符序号 (同一 f-string 内多次调用也各自递增, 不依赖 params 长度)。"""
        nonlocal _ph_n
        _ph_n += 1
        return f"${_ph_n}"

    if calc_date:
        conds.append(f"calc_date = {ph()}")
        params.append(calc_date)
    if keyword:
        conds.append(f"(name ILIKE {ph()} OR ts_code ILIKE {ph()})")
        params.append(f"%{keyword}%")
        params.append(f"%{keyword}%")
    if fund_type:
        conds.append(f"fund_type ILIKE {ph()}")
        params.append(f"%{fund_type}%")
    if min_scale is not None:
        conds.append(f"scale >= {ph()}")
        params.append(min_scale)
    if max_m_fee is not None:
        conds.append(f"m_fee <= {ph()}")
        params.append(max_m_fee)
    if max_c_fee is not None:
        conds.append(f"c_fee <= {ph()}")
        params.append(max_c_fee)
    if min_amount_20 is not None:
        conds.append(f"avg_amount_20 >= {ph()}")
        params.append(min_amount_20)
    if max_premium is not None:
        conds.append(f"ABS(premium) <= {ph()}")
        params.append(max_premium)
    if min_pos52 is not None:
        conds.append(f"pos52 >= {ph()}")
        params.append(min_pos52)
    if max_pos52 is not None:
        conds.append(f"pos52 <= {ph()}")
        params.append(max_pos52)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT {", ".join(_ETF_COLS)}
        FROM etf_screen
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT {ph()}::int;
    """
    params.append(int(limit))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 自定义策略 (策略 Hub, 按用户隔离) + 策略公司
# ---------------------------------------------------------------------------
CUSTOM_STRATEGY_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS custom_strategies (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    name        VARCHAR(64) NOT NULL,
    desc_text   TEXT DEFAULT '',
    category    VARCHAR(32) DEFAULT '我的策略',
    source      VARCHAR(16) DEFAULT '',      -- screener=从选股保存 / manual=手动
    filter_info TEXT DEFAULT '',             -- 来源选股的条件描述 (JSON 字符串)
    created_at  TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_custom_strat_user ON custom_strategies (user_id);
CREATE TABLE IF NOT EXISTS custom_strategy_stocks (
    id          BIGSERIAL PRIMARY KEY,
    strategy_id BIGINT NOT NULL,
    ts_code     VARCHAR(16) NOT NULL,
    name        VARCHAR(64) DEFAULT '',
    note        VARCHAR(255) DEFAULT '',
    added_at    TIMESTAMP DEFAULT now(),
    UNIQUE (strategy_id, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_custom_strat_stock ON custom_strategy_stocks (strategy_id);
"""


async def init_custom_strategy_schema() -> None:
    """创建自定义策略表 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CUSTOM_STRATEGY_SCHEMA_DDL)


async def create_custom_strategy(user_id: int, name: str, desc_text: str = "",
                                 category: str = "我的策略", source: str = "manual",
                                 filter_info: str = "") -> int:
    """创建自定义策略, 返回策略 id。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        sid = await conn.fetchval(
            "INSERT INTO custom_strategies (user_id, name, desc_text, category, source, filter_info) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            user_id, name, desc_text, category, source, filter_info)
    return int(sid)


async def list_custom_strategies(user_id: int) -> list[dict]:
    """列出指定用户的全部自定义策略 (含公司数量)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.id, c.name, c.desc_text, c.category, c.source, c.filter_info, c.created_at, "
            "       (SELECT count(*) FROM custom_strategy_stocks s WHERE s.strategy_id = c.id) AS stock_count "
            "FROM custom_strategies c WHERE c.user_id = $1 ORDER BY c.id DESC", user_id)
    return [dict(r) for r in rows]


async def get_custom_strategy(sid: int) -> dict | None:
    """按 id 查策略 (不限用户, 调用方自行校验归属)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, user_id, name, desc_text, category, source, filter_info, created_at "
            "FROM custom_strategies WHERE id = $1", sid)
    return dict(r) if r else None


async def update_custom_strategy(sid: int, user_id: int, name: str | None = None,
                                 desc_text: str | None = None, category: str | None = None) -> int:
    """更新自定义策略 (仅限本人), 返回受影响行数。"""
    sets, params = [], []
    if name is not None:
        sets.append("name = $%d" % (len(params) + 1)); params.append(name)
    if desc_text is not None:
        sets.append("desc_text = $%d" % (len(params) + 1)); params.append(desc_text)
    if category is not None:
        sets.append("category = $%d" % (len(params) + 1)); params.append(category)
    if not sets:
        return 0
    params += [sid, user_id]
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"UPDATE custom_strategies SET {', '.join(sets)} WHERE id = ${len(params) - 1} AND user_id = ${len(params)} RETURNING id",
            *params)
    return len(rows)


async def delete_custom_strategy(sid: int, user_id: int) -> int:
    """删除自定义策略及其全部公司 (仅限本人), 返回删除行数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM custom_strategy_stocks WHERE strategy_id = $1", sid)
            rows = await conn.fetch("DELETE FROM custom_strategies WHERE id = $1 AND user_id = $2 RETURNING id",
                                    sid, user_id)
    return len(rows)


async def add_strategy_stock(sid: int, ts_code: str, name: str = "", note: str = "") -> bool:
    """向策略添加公司 ((strategy_id, ts_code) 唯一), 返回是否为新插入。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO custom_strategy_stocks (strategy_id, ts_code, name, note) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (strategy_id, ts_code) DO NOTHING RETURNING id",
            sid, ts_code, name, note)
    return row is not None


async def remove_strategy_stock(sid: int, ts_code: str) -> int:
    """从策略移除公司, 返回删除行数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("DELETE FROM custom_strategy_stocks WHERE strategy_id = $1 AND ts_code = $2 RETURNING id",
                                sid, ts_code)
    return len(rows)


async def list_strategy_stocks(sid: int) -> list[dict]:
    """列出策略内全部公司 (按添加时间升序)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts_code, name, note, added_at FROM custom_strategy_stocks "
            "WHERE strategy_id = $1 ORDER BY added_at ASC, id ASC", sid)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 精选思想 (投资大师/方法 skill, 按用户隔离, 可增删改查)
# ---------------------------------------------------------------------------
INVEST_IDEAS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS invest_ideas (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    name        VARCHAR(64) NOT NULL,
    school      VARCHAR(32) DEFAULT '',
    tags        TEXT DEFAULT '',       -- JSON 数组字符串
    bio         TEXT DEFAULT '',       -- 简介
    principles  TEXT DEFAULT '',       -- 核心理念 (每行一条)
    created_at  TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_invest_ideas_user ON invest_ideas (user_id);
"""


async def init_invest_ideas_schema() -> None:
    """创建精选思想表 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(INVEST_IDEAS_SCHEMA_DDL)


async def count_invest_ideas(user_id: int) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM invest_ideas WHERE user_id = $1", user_id))


async def create_invest_idea(user_id: int, name: str, school: str = "", tags: str = "",
                             bio: str = "", principles: str = "") -> int:
    """创建精选思想 skill, 返回 id。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        sid = await conn.fetchval(
            "INSERT INTO invest_ideas (user_id, name, school, tags, bio, principles) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            user_id, name, school, tags, bio, principles)
    return int(sid)


async def list_invest_ideas(user_id: int) -> list[dict]:
    """列出指定用户的全部精选思想 (按 id 升序)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, school, tags, bio, principles, created_at "
            "FROM invest_ideas WHERE user_id = $1 ORDER BY id ASC", user_id)
    return [dict(r) for r in rows]


async def get_invest_idea(sid: int) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, user_id, name, school, tags, bio, principles, created_at "
            "FROM invest_ideas WHERE id = $1", sid)
    return dict(r) if r else None


async def update_invest_idea(sid: int, user_id: int, name: str | None = None,
                             school: str | None = None, tags: str | None = None,
                             bio: str | None = None, principles: str | None = None) -> int:
    """更新精选思想 (仅限本人), 返回受影响行数。"""
    sets, params = [], []
    for col, val in (("name", name), ("school", school), ("tags", tags),
                     ("bio", bio), ("principles", principles)):
        if val is not None:
            sets.append(f"{col} = $%d" % (len(params) + 1)); params.append(val)
    if not sets:
        return 0
    params += [sid, user_id]
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"UPDATE invest_ideas SET {', '.join(sets)} WHERE id = ${len(params) - 1} AND user_id = ${len(params)} RETURNING id",
            *params)
    return len(rows)


async def delete_invest_idea(sid: int, user_id: int) -> int:
    """删除精选思想 (仅限本人), 返回受影响行数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("DELETE FROM invest_ideas WHERE id = $1 AND user_id = $2 RETURNING id",
                                sid, user_id)
    return len(rows)


# ---------------------------------------------------------------------------
# 公司大事表 stock_events (网络搜索 + DeepSeek 总结, 前端详情页时间线展示)
# ---------------------------------------------------------------------------

STOCK_EVENTS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS stock_events (
    id          BIGSERIAL PRIMARY KEY,
    ts_code     VARCHAR(16)  NOT NULL,
    name        VARCHAR(64)  DEFAULT '',
    event_date  VARCHAR(16)  DEFAULT '',   -- YYYY-MM (可空, 未知日期)
    title       VARCHAR(255) NOT NULL,
    summary     TEXT         DEFAULT '',
    source      VARCHAR(16)  DEFAULT 'llm',
    updated_at  TIMESTAMP    DEFAULT now(),
    UNIQUE (ts_code, title)
);
CREATE INDEX IF NOT EXISTS idx_stock_events_code_date
    ON stock_events (ts_code, event_date);
"""


async def init_stock_events_schema() -> None:
    """创建公司大事表 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(STOCK_EVENTS_SCHEMA_DDL)


async def upsert_stock_events(ts_code: str, name: str, events: list[dict]) -> int:
    """按 (ts_code, title) upsert 写入公司大事, 返回写入行数。

    events: [{"date": "YYYY-MM" 或 "", "title": ..., "summary": ...}, ...]
    """
    if not events:
        return 0
    rows = [(ts_code, name, (e.get("date") or "")[:10], (e.get("title") or "")[:255],
             e.get("summary") or "", "llm") for e in events]
    sql = ("INSERT INTO stock_events (ts_code, name, event_date, title, summary, source) "
           "VALUES ($1,$2,$3,$4,$5,$6) "
           "ON CONFLICT (ts_code, title) DO UPDATE SET "
           "name=EXCLUDED.name, event_date=EXCLUDED.event_date, "
           "summary=EXCLUDED.summary, source=EXCLUDED.source, updated_at=now();")
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(sql, rows)
    return len(rows)


async def get_stock_events(ts_code: str) -> list[dict]:
    """查询某股票的公司大事 (按日期降序, 无日期排后)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_date, title, summary, source, updated_at "
            "FROM stock_events WHERE ts_code = $1 "
            "ORDER BY (event_date = '' OR event_date IS NULL), event_date DESC, id DESC",
            ts_code)
    return [dict(r) for r in rows]


async def count_stock_events(ts_code: str) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM stock_events WHERE ts_code = $1", ts_code))


async def delete_stock_events(ts_code: str) -> int:
    """删除某股票全部大事 (重新生成时先清空), 返回删除行数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("DELETE FROM stock_events WHERE ts_code = $1 RETURNING id", ts_code)
    return len(rows)


async def stock_codes_missing_events(codes: list[str]) -> list[str]:
    """返回列表中没有大事记录的 ts_code (供批量同步跳过已生成)。"""
    if not codes:
        return []
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ts_code FROM stock_events WHERE ts_code = ANY($1::text[])",
            list(codes))
    have = {str(r["ts_code"]) for r in rows}
    return [c for c in codes if c not in have]


async def query_event_titles(ts_code: str) -> list[str]:
    """查询某股票已入库大事的标题 (供增量生成去重)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT title FROM stock_events WHERE ts_code = $1", ts_code)
    return [str(r["title"]) for r in rows]


async def stock_events_update_candidates(codes: list[str], min_count: int = 20,
                                         monthly_days: int = 30) -> list[str]:
    """返回需要更新大事的 ts_code (增量/月度策略):

      - 无大事记录          → 需要 (首次生成)
      - 不足 min_count 条    → 需要 (增量补充)
      - 达到 min_count 条    → 仅当最近更新超过 monthly_days 天才需要 (月度更新)
      其余 (≥min_count 且近期更新过) 跳过。
    """
    if not codes:
        return []
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts_code, count(*) n, max(updated_at) latest "
            "FROM stock_events WHERE ts_code = ANY($1::text[]) "
            "GROUP BY ts_code", list(codes))
    have = {str(r["ts_code"]): {"n": int(r["n"]), "latest": r["latest"]} for r in rows}
    cutoff = datetime.now() - timedelta(days=int(monthly_days))
    out = []
    for c in codes:
        info = have.get(c)
        if info is None:
            out.append(c)                      # 无大事 → 首次
        elif info["n"] < min_count:
            out.append(c)                      # 不足阈值 → 增量
        elif info["latest"] is not None and info["latest"] < cutoff:
            out.append(c)                      # 达到阈值但超月度周期 → 月度
    return out


async def has_any_financial(ts_code: str) -> bool:
    """该股票是否已有任一财年财务数据 (financial_data)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 FROM financial_data WHERE ts_code=$1 LIMIT 1", ts_code) is not None


async def financial_years(ts_code: str) -> list[int]:
    """查询该股票 financial_data 中已入库的年份列表。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT year FROM financial_data WHERE ts_code = $1", ts_code)
    return [int(r["year"]) for r in rows]


# ---------------------------------------------------------------------------
# 公司大事任务队列 stock_events_jobs (持久化, 替代进程内 asyncio.create_task)
# 解决: 服务器多进程/重启时后台任务丢失; 任务存 pg, 由 worker 循环抢占处理。
# ---------------------------------------------------------------------------

STOCK_EVENTS_JOBS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS stock_events_jobs (
    id          BIGSERIAL PRIMARY KEY,
    ts_code     VARCHAR(16) NOT NULL UNIQUE,
    name        VARCHAR(64) DEFAULT '',
    status      VARCHAR(16) DEFAULT 'pending',   -- pending / processing / done / error
    done_count  INTEGER DEFAULT 0,               -- 已入库条数 (分批生成进度)
    total_est   INTEGER DEFAULT 0,               -- 预估总条数
    last_error  TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT now(),
    updated_at  TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sej_status ON stock_events_jobs (status, id);
"""


async def init_stock_events_jobs_schema() -> None:
    """创建公司大事任务队列表 (幂等)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(STOCK_EVENTS_JOBS_SCHEMA_DDL)


async def enqueue_stock_events_job(ts_code: str, name: str = "", force: bool = False) -> bool:
    """入队生成任务 (ts_code 唯一)。force=True 时重置状态为 pending 并清空错误。

    返回是否为新插入/重置 (供调用方判断是否需要重新触发)。
    """
    if force:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE stock_events_jobs SET status='pending', done_count=0, "
                "total_est=0, last_error='', updated_at=now() WHERE ts_code=$1 RETURNING id",
                ts_code)
            if rows:
                return True
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO stock_events_jobs (ts_code, name, status) VALUES ($1, $2, 'pending') "
            "ON CONFLICT (ts_code) DO NOTHING RETURNING id", ts_code, name)
    return row is not None


async def get_stock_events_job(ts_code: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT ts_code, status, done_count, total_est, last_error, updated_at "
            "FROM stock_events_jobs WHERE ts_code = $1", ts_code)
    return dict(r) if r else None


async def claim_stock_events_job(stale_minutes: int = 5) -> dict | None:
    """抢占一个待处理任务 (多 worker 并发安全, FOR UPDATE SKIP LOCKED)。

    同时把卡死超过 stale_minutes 的 processing 任务重置为 pending (进程崩溃恢复)。
    返回任务 {ts_code, name} 或 None。
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 恢复卡死任务
        await conn.execute(
            "UPDATE stock_events_jobs SET status='pending', updated_at=now() "
            "WHERE status='processing' AND updated_at < now() - make_interval(mins => $1)",
            stale_minutes)
        r = await conn.fetchrow(
            "UPDATE stock_events_jobs SET status='processing', updated_at=now() "
            "WHERE id = (SELECT id FROM stock_events_jobs WHERE status='pending' "
            "             ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING ts_code, name")
    return dict(r) if r else None


async def update_stock_events_job(ts_code: str, status: str | None = None,
                                  done_count: int | None = None,
                                  total_est: int | None = None,
                                  last_error: str = "") -> None:
    """更新任务状态/进度/错误。last_error 传 None 表示不改, 传字符串表示覆盖。"""
    sets = ["updated_at = now()"]
    params: list = []
    if status is not None:
        params.append(status)
        sets.append(f"status = ${len(params)}")
    if done_count is not None:
        params.append(done_count)
        sets.append(f"done_count = ${len(params)}")
    if total_est is not None:
        params.append(total_est)
        sets.append(f"total_est = ${len(params)}")
    if last_error is not None:
        params.append(last_error)
        sets.append(f"last_error = ${len(params)}")
    if not sets:
        return
    params.append(ts_code)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE stock_events_jobs SET {', '.join(sets)} "
            f"WHERE ts_code = ${len(params)}", *params)


async def pending_stock_events_job_count() -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM stock_events_jobs WHERE status = 'pending'"))


# ---------------------------------------------------------------------------
# 公司官网内容表 site_pages (愿景/使命/价值观/新闻, 后台页面管理编辑)
# 单行表: 全站内容存 JSON 字段, 前端官网 fetch 渲染, 后台 tab 编辑保存
# ---------------------------------------------------------------------------

SITE_PAGES_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS site_pages (
    id          BIGSERIAL PRIMARY KEY,
    key         VARCHAR(32)  NOT NULL UNIQUE,   -- vision / mission / values / industry_research / news
    title       VARCHAR(64)  DEFAULT '',
    content     TEXT         DEFAULT '',        -- vision/mission 为 JSON; industry_research/news 为 Markdown 文本
    content_en  TEXT         DEFAULT '',        -- 英文内容 (与 content 同格式, 空=无英文版)
    updated_at  TIMESTAMP    DEFAULT now()
);
"""

# 各内容块的默认占位 (插入/补齐时使用)
SITE_PAGES_DEFAULTS = [
    ("vision", "愿景",
     '{"title": "愿景", "lede": "创造时间和知识的复利。", '
     '"body": "时间是价值投资最重要的盟友，知识是做出正确判断最可靠的根基。我们相信，真正的复利不止来自资本，更来自长期积累的正确认知与时间的朋友。让每一次决策都沉淀为可复用的知识，让每一分时间都服务于长期价值——这是我们为之努力的愿景。"}'),
    ("mission", "使命",
     '{"title": "使命", "lede": "一视同仁，为客户创造合理的、可持续的、超过社会平均的收益。", '
     '"body": "无论资金规模大小，我们都以同样的审慎与纪律对待每一份托付。合理的收益意味着不追逐泡沫与侥幸，可持续的收益意味着拥有穿越周期的稳健底色，超过社会平均的收益意味着持续为客户创造真正的增量价值。"}'),
    ("values", "价值观",
     '[{"name": "本分", "desc": "做对的事情、把事情做对、求责于己，坚持 stop doing list——知道什么不该做，比知道该做什么更重要。"},'
     '{"name": "客观", "desc": "假设正确、逻辑正确、事实正确；对抗认知偏差、对抗误判心理，让决策建立在可验证的事实与逻辑之上。"},'
     '{"name": "理性", "desc": "避免愚蠢，而非追求聪明；避免损失，而非追求利润。把不犯重大错误作为第一原则。"},'
     '{"name": "诚实", "desc": "对知识、对人诚实，知之为知之，不知为不知。不掩饰无知，不夸大能力，对结果如实相告。"},'
     '{"name": "持续学习 · 知行合一", "desc": "把学习作为终身习惯，在不确定性中持续进化；让认知落到行动，让行动验证认知，循环精进。"}]'),
    ("industry_research", "行业研究",
     '# 行业研究\n\n我们持续对重点行业与企业展开深度研究，以下为部分研究成果。\n\n## 白酒行业深度\n\n高端化与库存周期……\n'),
    ("news", "新闻浏览",
     '# 新闻浏览\n\n## 研究平台上线\n\n财宝资本研究平台全面上线智能选股回测系统。\n'),
]


async def init_site_pages_schema() -> None:
    """创建 site_pages 表 (幂等)。首次空表写入全部默认内容;
    已有表仅补齐缺失的 key (如从旧版升级新增 research 块, 不覆盖用户编辑)。
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SITE_PAGES_SCHEMA_DDL)
        # 迁移: 新增 content_en 列 (双语)
        await conn.execute("ALTER TABLE site_pages ADD COLUMN IF NOT EXISTS content_en TEXT DEFAULT '';")
        # 旧版迁移: research → industry_research (改名)
        has_research = await conn.fetchval("SELECT 1 FROM site_pages WHERE key='research' LIMIT 1")
        if has_research:
            await conn.execute("UPDATE site_pages SET key='industry_research', title='行业研究' "
                               "WHERE key='research'")
        # 旧版迁移: news 为 JSON 数组格式 → 转 markdown (旧列表 [{date,title,tag},...])
        news_row = await conn.fetchrow("SELECT content FROM site_pages WHERE key='news'")
        if news_row and news_row["content"] and str(news_row["content"]).lstrip().startswith("["):
            try:
                import json as _json
                old_news = _json.loads(news_row["content"])
                if isinstance(old_news, list):
                    lines = ["# 新闻浏览", ""]
                    for n in old_news:
                        title = (n.get("title") or "").strip()
                        date = (n.get("date") or "").strip()
                        if title:
                            lines.append(f"## {title}")
                            if date:
                                lines.append(f"*{date}*")
                            if n.get("tag"):
                                lines.append(f"标签: {n.get('tag')}")
                            lines.append("")
                    await conn.execute("UPDATE site_pages SET content=$1, updated_at=now() "
                                       "WHERE key='news'", "\n".join(lines))
            except Exception:
                pass  # 非 JSON 数组则跳过 (已是 markdown)
        # 已存在的 key 集合
        rows = await conn.fetch("SELECT key FROM site_pages")
        have = {str(r["key"]) for r in rows}
        # 插入缺失的默认块
        missing = [(k, t, c) for k, t, c in SITE_PAGES_DEFAULTS if k not in have]
        if missing:
            await conn.executemany(
                "INSERT INTO site_pages (key, title, content) VALUES ($1, $2, $3)",
                missing)


async def get_site_pages() -> dict[str, dict]:
    """读取全部官网内容, 返回 {key: {title, content, content_en, updated_at}}。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, title, content, content_en, updated_at FROM site_pages")
    return {str(r["key"]): {"title": r["title"], "content": r["content"],
                            "content_en": r["content_en"] or "",
                            "updated_at": str(r["updated_at"])} for r in rows}


async def upsert_site_page(key: str, title: str, content: str, content_en: str = "") -> bool:
    """写入/更新一个官网内容块 (key 唯一 upsert, 双语)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO site_pages (key, title, content, content_en, updated_at) "
            "VALUES ($1, $2, $3, $4, now()) "
            "ON CONFLICT (key) DO UPDATE SET title = EXCLUDED.title, "
            "content = EXCLUDED.content, content_en = EXCLUDED.content_en, updated_at = now()",
            key, title, content, content_en or "")
    return True


# ---------------------------------------------------------------------------
# 官网文章表 site_articles (行业研究 / 新闻浏览, 后台文章管理增删改查)
# 前端 /research /news 显示文章列表(标题/日期/标签), 点击进正文页
# ---------------------------------------------------------------------------

SITE_ARTICLES_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS site_articles (
    id          BIGSERIAL PRIMARY KEY,
    kind        VARCHAR(16)  NOT NULL,   -- research 行业研究 / news 新闻浏览
    title       VARCHAR(200) NOT NULL,   -- 标题 (列表展示, 点击进正文) 中文
    title_en    VARCHAR(200) DEFAULT '', -- 英文标题
    date        VARCHAR(32)  DEFAULT '', -- 显示日期 (如 2026-08-26)
    tags        TEXT         DEFAULT '', -- 标签 (JSON 数组字符串, 列表展示)
    body        TEXT         DEFAULT '', -- 正文 (Markdown, 正文页渲染) 中文
    body_en     TEXT         DEFAULT '', -- 英文正文 (Markdown)
    created_at  TIMESTAMP    DEFAULT now(),
    updated_at  TIMESTAMP    DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_site_articles_kind ON site_articles (kind, id);
"""


async def init_site_articles_schema() -> None:
    """创建 site_articles 表 (幂等)。首次空表时从旧 site_pages 迁移内容。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SITE_ARTICLES_SCHEMA_DDL)
        # 迁移: 新增 title_en / body_en 列 (双语)
        await conn.execute("ALTER TABLE site_articles ADD COLUMN IF NOT EXISTS title_en VARCHAR(200) DEFAULT '';")
        await conn.execute("ALTER TABLE site_articles ADD COLUMN IF NOT EXISTS body_en TEXT DEFAULT '';")
        # 首次迁移: 表为空时, 将旧 site_pages 的行业研究/新闻 markdown 拆成文章
        n = await conn.fetchval("SELECT count(*) FROM site_articles")
        if n == 0:
            for key, kind in (("industry_research", "research"), ("news", "news")):
                row = await conn.fetchrow(
                    "SELECT title, content FROM site_pages WHERE key=$1", key)
                if not row or not row["content"]:
                    continue
                title = str(row["title"]) or ("行业研究" if kind == "research" else "新闻浏览")
                # 解析 markdown: 二级标题"## X" 为文章标题, 其后内容为正文
                lines = str(row["content"]).splitlines()
                current_title = ""
                buf: list[str] = []
                for ln in lines:
                    if ln.startswith("## "):
                        # 提交上一篇文章
                        if current_title:
                            await conn.execute(
                                "INSERT INTO site_articles (kind, title, date, tags, body) "
                                "VALUES ($1, $2, $3, $4, $5)",
                                kind, current_title, "", "", "\n".join(buf).strip())
                        current_title = ln[3:].strip()
                        buf = []
                    elif not current_title:
                        continue  # 忽略 ## 之前的内容
                    else:
                        buf.append(ln)
                if current_title:
                    await conn.execute(
                        "INSERT INTO site_articles (kind, title, date, tags, body) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        kind, current_title, "", "", "\n".join(buf).strip())
                if not current_title and not buf:
                    # 全篇无 ## 标题: 整篇作为一篇文章
                    await conn.execute(
                        "INSERT INTO site_articles (kind, title, date, tags, body) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        kind, title, "", "", str(row["content"]).strip())


async def list_site_articles(kind: str, limit: int = 100) -> list[dict]:
    """列出某类型的全部文章 (按 id 降序, 列表页用, 不含 body 以减负)。"""
    if kind not in ("research", "news"):
        kind = "research"
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, kind, title, title_en, date, tags, updated_at "
            "FROM site_articles WHERE kind = $1 ORDER BY id DESC LIMIT $2::int",
            kind, int(limit))
    return [dict(r) for r in rows]


async def get_site_article(aid: int) -> dict | None:
    """按 id 查文章 (正文页用, 含 body)。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, kind, title, title_en, date, tags, body, body_en, updated_at "
            "FROM site_articles WHERE id = $1", aid)
    return dict(r) if r else None


async def create_site_article(kind: str, title: str, date: str, tags: str,
                              body: str, title_en: str = "", body_en: str = "") -> int:
    """创建文章, 返回 id。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        aid = await conn.fetchval(
            "INSERT INTO site_articles (kind, title, title_en, date, tags, body, body_en) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            kind, title, title_en or "", date, tags, body, body_en or "")
    return int(aid)


async def update_site_article(aid: int, kind: str, title: str, date: str,
                              tags: str, body: str, title_en: str = "",
                              body_en: str = "") -> int:
    """更新文章, 返回受影响行数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE site_articles SET kind=$1, title=$2, title_en=$3, date=$4, tags=$5, "
            "body=$6, body_en=$7, updated_at=now() WHERE id=$8 RETURNING id",
            kind, title, title_en or "", date, tags, body, body_en or "", aid)
    return len(rows)


async def delete_site_article(aid: int) -> int:
    """删除文章, 返回受影响行数。"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("DELETE FROM site_articles WHERE id=$1 RETURNING id", aid)
    return len(rows)
