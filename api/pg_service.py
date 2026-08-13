"""PostgreSQL 存取层: 红利低波选股数据表 red_low_vol。

连接串从根目录 .env 的 DATABASE_URL 读取 (默认本机 Postgres.app)。
表按 (ts_code, year) 唯一, 数据通过 upsert 幂等写入。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
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
