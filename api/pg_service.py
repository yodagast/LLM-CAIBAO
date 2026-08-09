"""PostgreSQL 存取层: 红利低波选股数据表 red_low_vol。

连接串从根目录 .env 的 DATABASE_URL 读取 (默认本机 Postgres.app)。
表按 (ts_code, year) 唯一, 数据通过 upsert 幂等写入。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 允许前端排序的字段白名单 (防 SQL 注入)
SORTABLE_COLUMNS = {
    "year": "year",
    "name": "name",
    "dividend_yield": "dividend_yield",
    "dividend_yield_ttm": "dividend_yield_ttm",
    "volatility": "volatility",
    "div_per_share": "div_per_share",
    "free_cashflow": "free_cashflow",
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


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(_dsn())


# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------

def init_schema() -> None:
    """创建 red_low_vol 表与索引 (幂等), 并对旧表迁移新增列。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)
            # 旧表迁移: 新增 股息率-TTM / 上个交易日收盘价 列
            cur.execute("ALTER TABLE red_low_vol ADD COLUMN IF NOT EXISTS dividend_yield_ttm DOUBLE PRECISION;")
            cur.execute("ALTER TABLE red_low_vol ADD COLUMN IF NOT EXISTS last_close DOUBLE PRECISION;")
        conn.commit()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

_UPSERT_COLS = [
    "ts_code", "symbol", "name", "industry", "year",
    "dividend_yield", "dividend_yield_ttm", "last_close", "volatility", "div_per_share", "free_cashflow", "eps",
    "payout_ratio", "dividend_growth_3y", "roe", "debt_to_assets",
    "avg_daily_mv", "avg_daily_amt", "end_date",
]

_UPSERT_SQL = f"""
INSERT INTO red_low_vol ({", ".join(_UPSERT_COLS)})
VALUES ({", ".join("%(" + c + ")s" for c in _UPSERT_COLS)})
ON CONFLICT (ts_code, year) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in _UPSERT_COLS if c not in ("ts_code", "year"))},
updated_at = now();
"""


def upsert_rows(rows: list[dict]) -> int:
    """按 (ts_code, year) upsert 写入, 返回写入行数。"""
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                params = {c: r.get(c) for c in _UPSERT_COLS}
                cur.execute(_UPSERT_SQL, params)
        conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def has_data(industry: str, year: int) -> bool:
    """该行业+年份是否已有数据 (行业子串匹配)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM red_low_vol WHERE industry LIKE %s AND year = %s LIMIT 1;",
                (f"%{industry}%", year),
            )
            return cur.fetchone() is not None


def count_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配, 与同步 str.contains 一致)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM red_low_vol WHERE industry LIKE %s AND year = %s;",
                (f"%{industry}%", year),
            )
            return int(cur.fetchone()[0])


def query_screen(industry: str, years: list[int], sort_by: str = "dividend_yield",
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
    if industry:
        # 子串匹配, 与同步逻辑 str.contains 一致 (如输入"电力"匹配"新型电力")
        conds.append("industry LIKE %s")
        params.append(f"%{industry}%")
    if years:
        conds.append("year = ANY(%s)")
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
            conds.append(f"{col_name} >= %s")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= %s")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, year,
               dividend_yield, dividend_yield_ttm, last_close, volatility, div_per_share, free_cashflow, eps,
               payout_ratio, dividend_growth_3y, roe, debt_to_assets,
               avg_daily_mv, avg_daily_amt, end_date
        FROM red_low_vol
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT %s;
    """
    params.append(int(limit))
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
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
    "debt_to_assets": "debt_to_assets",
    "total_cur_assets": "total_cur_assets",
    "money_cap": "money_cap",
    "invturn_days": "invturn_days",
    "arturn_days": "arturn_days",
}

_FUND_COLS = [
    "ts_code", "symbol", "name", "industry", "year",
    "close", "roe", "net_margin", "assets_turn", "equity_multiplier",
    "gross_margin", "debt_to_assets", "total_cur_assets", "money_cap",
    "invturn_days", "arturn_days", "end_date",
]

_FUND_UPSERT_SQL = f"""
INSERT INTO fundamental_screen ({", ".join(_FUND_COLS)})
VALUES ({", ".join("%(" + c + ")s" for c in _FUND_COLS)})
ON CONFLICT (ts_code, year) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in _FUND_COLS if c not in ("ts_code", "year"))},
updated_at = now();
"""


def init_fundamental_schema() -> None:
    """创建 fundamental_screen 表与索引 (幂等)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(FUNDAMENTAL_SCHEMA_DDL)
        conn.commit()


def upsert_fundamental_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(_FUND_UPSERT_SQL, {c: r.get(c) for c in _FUND_COLS})
        conn.commit()
    return len(rows)


def count_fundamental_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM fundamental_screen WHERE industry LIKE %s AND year = %s;",
                (f"%{industry}%", year),
            )
            return int(cur.fetchone()[0])


def query_fundamental(industry: str, years: list[int], sort_by: str = "roe",
                      order: str = "desc", limit: int = 1000,
                      filters: dict | None = None) -> list[dict]:
    """按行业+多年份查询基本面数据, 支持阈值筛选与排序。"""
    col = FUNDAMENTAL_SORTABLE_COLUMNS.get(sort_by, "roe")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []
    if industry:
        # 子串匹配, 与同步逻辑 str.contains 一致
        conds.append("industry LIKE %s")
        params.append(f"%{industry}%")
    if years:
        conds.append("year = ANY(%s)")
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
            conds.append(f"{col_name} >= %s")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= %s")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, year, close, roe, net_margin,
               assets_turn, equity_multiplier, gross_margin, debt_to_assets,
               total_cur_assets, money_cap, invturn_days, arturn_days, end_date
        FROM fundamental_screen
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT %s;
    """
    params.append(int(limit))
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
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

_FIN_UPSERT_SQL = f"""
INSERT INTO financial_data ({", ".join(FINANCIAL_COLS)})
VALUES ({", ".join("%(" + c + ")s" for c in FINANCIAL_COLS)})
ON CONFLICT (ts_code, year) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in FINANCIAL_COLS if c not in ("ts_code", "year"))},
updated_at = now();
"""


def init_financial_schema() -> None:
    """创建 financial_data 表与索引 (幂等)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(FINANCIAL_SCHEMA_DDL)
        conn.commit()


def upsert_financial_rows(rows: list[dict]) -> int:
    """按 (ts_code, year) upsert 写入财务数据, 返回行数。"""
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(_FIN_UPSERT_SQL, {c: r.get(c) for c in FINANCIAL_COLS})
        conn.commit()
    return len(rows)


def has_financial(ts_code: str, year: int) -> bool:
    """该股票该年份是否已有财务数据。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM financial_data WHERE ts_code=%s AND year=%s LIMIT 1;",
                        (ts_code, int(year)))
            return cur.fetchone() is not None


def query_financial_by_code(ts_code: str, years: list[int]) -> list[dict]:
    """查询某股票多年财务数据 (年报)。"""
    if not years:
        return []
    cols = ", ".join(FINANCIAL_COLS)
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT {cols} FROM financial_data "
                "WHERE ts_code=%s AND year = ANY(%s) ORDER BY year;",
                (ts_code, [int(y) for y in years]))
            rows = cur.fetchall()
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

_DR_UPSERT_SQL = f"""
INSERT INTO daily_band_recommend ({", ".join(DAILY_REC_COLS)})
VALUES ({", ".join("%(" + c + ")s" for c in DAILY_REC_COLS)})
ON CONFLICT (calc_date, ts_code) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in DAILY_REC_COLS if c not in ("calc_date", "ts_code"))},
updated_at = now();
"""


def init_daily_rec_schema() -> None:
    """创建 daily_band_recommend 表与索引 (幂等), 旧表自动补 industry 列。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(DAILY_REC_SCHEMA_DDL)
            # 旧表迁移: 补 industry 列
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'daily_band_recommend' AND column_name = 'industry'
            """)
            if cur.fetchone() is None:
                cur.execute("ALTER TABLE daily_band_recommend ADD COLUMN industry VARCHAR(64) DEFAULT ''")
        conn.commit()


def upsert_daily_rec_rows(rows: list[dict]) -> int:
    """按 (calc_date, ts_code) upsert 写入每日推荐, 返回行数。"""
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(_DR_UPSERT_SQL, {c: r.get(c) for c in DAILY_REC_COLS})
        conn.commit()
    return len(rows)


def has_daily_rec(calc_date: str, industry: str = "") -> int:
    """统计某计算日 (可选行业) 已入库的每日推荐行数; 用于缓存命中判断。"""
    if not calc_date:
        return 0
    sql = "SELECT COUNT(*) FROM daily_band_recommend WHERE calc_date = %s"
    params: list = [calc_date]
    if industry.strip():
        sql += " AND industry LIKE %s"
        params.append(f"%{industry.strip()}%")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()[0] or 0)


def daily_rec_done_codes(calc_date: str) -> list:
    """某计算日已入库的 ts_code 列表 (用于全市场续跑跳过)。"""
    if not calc_date:
        return []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ts_code FROM daily_band_recommend WHERE calc_date = %s",
                        (calc_date,))
            return [str(r[0]) for r in cur.fetchall()]


def backfill_daily_rec_industry(ts_code_industry: dict) -> int:
    """回填 daily_band_recommend 中 industry 为空的行 (按 ts_code 映射), 返回更新行数。"""
    if not ts_code_industry:
        return 0
    n = 0
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, ts_code FROM daily_band_recommend "
                        "WHERE industry IS NULL OR industry = ''")
            for row_id, ts_code in cur.fetchall():
                ind = ts_code_industry.get(str(ts_code), "")
                if ind:
                    cur.execute("UPDATE daily_band_recommend SET industry=%s WHERE id=%s",
                                (ind, row_id))
                    n += 1
        conn.commit()
    return n


def latest_calc_date() -> str:
    """最近一次计算的 calc_date。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(calc_date) FROM daily_band_recommend;")
            return str(cur.fetchone()[0] or "")


def query_daily_recommend(calc_date: str | None = None, buy_above_close: bool = True,
                          limit: int = 500, industry: str = "") -> list[dict]:
    """查询某计算日的推荐 (buy_price >= close), 按 close 降序; industry 非空时按行业子串过滤。"""
    if not calc_date:
        calc_date = latest_calc_date()
    if not calc_date:
        return []
    sql = "SELECT * FROM daily_band_recommend WHERE calc_date = %s"
    params: list = [calc_date]
    if buy_above_close:
        sql += " AND buy_price >= close"
    if industry.strip():
        sql += " AND industry LIKE %s"
        params.append(f"%{industry.strip()}%")
    sql += " ORDER BY close DESC LIMIT %s"
    params.append(int(limit))
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def latest_rlv_year() -> int:
    """red_low_vol 最新数据年份。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(year) FROM red_low_vol;")
            return int(cur.fetchone()[0] or 0)


def query_dividend_recommend(min_dy_ttm: float = 3.0, industry: str = "",
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
        year_conds.append("year >= %s")
        year_params.append(int(year_min))
    if year_max:
        year_conds.append("year <= %s")
        year_params.append(int(year_max))
    if not year_conds:
        latest = latest_rlv_year()
        if not latest:
            return []
        year_conds.append("year = %s")
        year_params.append(latest)
    sql = ("SELECT ts_code, symbol, name, industry, year, "
           "dividend_yield, dividend_yield_ttm, last_close, volatility, div_per_share, "
           "payout_ratio, dividend_growth_3y, roe, debt_to_assets "
           "FROM red_low_vol WHERE " + " AND ".join(year_conds))
    params: list = year_params
    if industry.strip():
        sql += " AND industry LIKE %s"
        params.append(f"%{industry.strip()}%")
    if min_dy_ttm is not None and float(min_dy_ttm) > 0:
        sql += " AND dividend_yield_ttm >= %s"
        params.append(float(min_dy_ttm))
    if payout_min is not None:
        sql += " AND payout_ratio >= %s"
        params.append(float(payout_min))
    if payout_max is not None:
        sql += " AND payout_ratio <= %s"
        params.append(float(payout_max))
    if roe_min is not None:
        sql += " AND roe >= %s"
        params.append(float(roe_min))
    if roe_max is not None:
        sql += " AND roe <= %s"
        params.append(float(roe_max))
    sql += " ORDER BY dividend_yield_ttm DESC NULLS LAST, ts_code ASC LIMIT %s"
    params.append(int(limit))
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 我的股票表 my_stocks (自选股, 从股票详情页添加)
# ---------------------------------------------------------------------------

MY_STOCKS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS my_stocks (
    id          BIGSERIAL PRIMARY KEY,
    ts_code     VARCHAR(16)  NOT NULL UNIQUE,
    name        VARCHAR(64)  NOT NULL DEFAULT '',
    added_at    TIMESTAMP    DEFAULT now()
);
"""


def init_my_stocks_schema() -> None:
    """创建 my_stocks 表 (幂等)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(MY_STOCKS_SCHEMA_DDL)
        conn.commit()


def add_my_stock(ts_code: str, name: str) -> bool:
    """添加自选股 (ts_code 唯一), 返回是否为新插入 (已存在返回 False)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO my_stocks (ts_code, name) VALUES (%s, %s) "
                "ON CONFLICT (ts_code) DO NOTHING",
                (ts_code, name),
            )
            inserted = cur.rowcount > 0
        conn.commit()
    return inserted


def remove_my_stock(ts_code: str) -> int:
    """移除自选股, 返回删除行数。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM my_stocks WHERE ts_code = %s", (ts_code,))
            n = cur.rowcount
        conn.commit()
    return n


def list_my_stocks() -> list[dict]:
    """列出全部自选股 (按添加时间升序)。"""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT ts_code, name, added_at FROM my_stocks ORDER BY added_at ASC, id ASC")
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def has_my_stock(ts_code: str) -> bool:
    """某股票是否已在自选股中。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM my_stocks WHERE ts_code = %s LIMIT 1", (ts_code,))
            return cur.fetchone() is not None


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
    "dividend_yield", "dividend_yield_ttm", "last_close", "volatility", "div_per_share", "free_cashflow",
    "eps", "payout_ratio", "dividend_growth_3y", "roe", "debt_to_assets",
    "avg_daily_mv", "avg_daily_amt", "end_date",
]

_HK_RLV_UPSERT_SQL = f"""
INSERT INTO hk_red_low_vol ({", ".join(_HK_RLV_COLS)})
VALUES ({", ".join("%(" + c + ")s" for c in _HK_RLV_COLS)})
ON CONFLICT (ts_code, year) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in _HK_RLV_COLS if c not in ("ts_code", "year"))},
updated_at = now();
"""


def init_hk_rlv_schema() -> None:
    """创建 hk_red_low_vol 表与索引 (幂等)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(HK_RLV_SCHEMA_DDL)
        conn.commit()


def upsert_hk_rlv_rows(rows: list[dict]) -> int:
    """按 (ts_code, year) upsert 写入港股红利低波, 返回行数。"""
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(_HK_RLV_UPSERT_SQL, {c: r.get(c) for c in _HK_RLV_COLS})
        conn.commit()
    return len(rows)


def count_hk_rlv_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM hk_red_low_vol WHERE industry LIKE %s AND year = %s;",
                (f"%{industry}%", year),
            )
            return int(cur.fetchone()[0])


def query_hk_rlv(industry: str, years: list[int], sort_by: str = "dividend_yield",
                 order: str = "desc", limit: int = 500,
                 filters: dict | None = None) -> list[dict]:
    """港股红利低波: 按行业+多年份查询, 支持阈值筛选与排序 (字段白名单防注入)。"""
    col = HK_RLV_SORTABLE_COLUMNS.get(sort_by, "dividend_yield")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []
    if industry:
        conds.append("industry LIKE %s")
        params.append(f"%{industry}%")
    if years:
        conds.append("year = ANY(%s)")
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
            conds.append(f"{col_name} >= %s")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= %s")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, market, year,
               dividend_yield, dividend_yield_ttm, last_close, volatility, div_per_share, free_cashflow,
               eps, payout_ratio, dividend_growth_3y, roe, debt_to_assets,
               avg_daily_mv, avg_daily_amt, end_date
        FROM hk_red_low_vol
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT %s;
    """
    params.append(int(limit))
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def latest_hk_rlv_year() -> int:
    """hk_red_low_vol 最新数据年份。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(year) FROM hk_red_low_vol;")
            return int(cur.fetchone()[0] or 0)


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
    "gross_margin", "debt_to_assets", "current_ratio", "total_cur_assets", "money_cap",
    "invturn_days", "arturn_days", "eps", "operate_income", "net_profit", "total_mv", "end_date",
]

_HK_FUND_UPSERT_SQL = f"""
INSERT INTO hk_fundamental_screen ({", ".join(_HK_FUND_COLS)})
VALUES ({", ".join("%(" + c + ")s" for c in _HK_FUND_COLS)})
ON CONFLICT (ts_code, year) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in _HK_FUND_COLS if c not in ("ts_code", "year"))},
updated_at = now();
"""


def init_hk_fundamental_schema() -> None:
    """创建 hk_fundamental_screen 表与索引 (幂等)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(HK_FUNDAMENTAL_SCHEMA_DDL)
        conn.commit()


def upsert_hk_fundamental_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(_HK_FUND_UPSERT_SQL, {c: r.get(c) for c in _HK_FUND_COLS})
        conn.commit()
    return len(rows)


def count_hk_fundamental_by_industry_year(industry: str, year: int) -> int:
    """该行业+年份的记录数 (行业子串匹配)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM hk_fundamental_screen WHERE industry LIKE %s AND year = %s;",
                (f"%{industry}%", year),
            )
            return int(cur.fetchone()[0])


def query_hk_fundamental(industry: str, years: list[int], sort_by: str = "roe",
                         order: str = "desc", limit: int = 1000,
                         filters: dict | None = None) -> list[dict]:
    """港股基本面: 按行业+多年份查询, 支持阈值筛选与排序。"""
    col = HK_FUNDAMENTAL_SORTABLE_COLUMNS.get(sort_by, "roe")
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    conds: list[str] = []
    params: list = []
    if industry:
        conds.append("industry LIKE %s")
        params.append(f"%{industry}%")
    if years:
        conds.append("year = ANY(%s)")
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
            conds.append(f"{col_name} >= %s")
            params.append(mn)
        if mx is not None:
            conds.append(f"{col_name} <= %s")
            params.append(mx)

    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        SELECT ts_code, symbol, name, industry, market, year, close, roe, net_margin,
               assets_turn, equity_multiplier, gross_margin, debt_to_assets, current_ratio,
               total_cur_assets, money_cap, invturn_days, arturn_days,
               eps, operate_income, net_profit, total_mv, end_date
        FROM hk_fundamental_screen
        {where_sql}
        ORDER BY {col} {order_sql} NULLS LAST, ts_code ASC
        LIMIT %s;
    """
    params.append(int(limit))
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def hk_synced_ts_codes(years: list[int]) -> set:
    """港股两表在给定年份全部有数据的 ts_code 集合 (全市场续跑断点用)。

    某股票被视为「已同步」= hk_red_low_vol 与 hk_fundamental_screen 中,
    给定年份全部存在 (count(DISTINCT year) = len(years))。
    """
    years = [int(y) for y in years]
    if not years:
        return set()
    result: set | None = None
    for table in ("hk_red_low_vol", "hk_fundamental_screen"):
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT ts_code FROM {table} WHERE year = ANY(%s) "
                    "GROUP BY ts_code HAVING count(DISTINCT year) = %s",
                    (years, len(years)),
                )
                s = {str(r[0]) for r in cur.fetchall()}
        result = s if result is None else (result & s)
    return result or set()
