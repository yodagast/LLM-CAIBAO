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
    "dividend_yield": "dividend_yield",
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
}

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS red_low_vol (
    id                    BIGSERIAL PRIMARY KEY,
    ts_code               VARCHAR(16)  NOT NULL,
    symbol                VARCHAR(8)   NOT NULL,
    name                  VARCHAR(64)  NOT NULL,
    industry              VARCHAR(32)  DEFAULT '',
    year                  INTEGER      NOT NULL,
    dividend_yield        DOUBLE PRECISION,   -- 股息率 % (当年每股分红 / 年末收盘价)
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
    """创建 red_low_vol 表与索引 (幂等)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)
        conn.commit()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

_UPSERT_COLS = [
    "ts_code", "symbol", "name", "industry", "year",
    "dividend_yield", "volatility", "div_per_share", "free_cashflow", "eps",
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
               dividend_yield, volatility, div_per_share, free_cashflow, eps,
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
# 财报分析数据表 financial_data (tushare 年度财务指标)
# ---------------------------------------------------------------------------

FINANCIAL_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS financial_data (
    id                    BIGSERIAL PRIMARY KEY,
    ts_code               VARCHAR(16)  NOT NULL,
    symbol                VARCHAR(8)   NOT NULL,
    name                  VARCHAR(64)  NOT NULL,
    industry              VARCHAR(32)  DEFAULT '',
    year                  INTEGER      NOT NULL,
    total_revenue         DOUBLE PRECISION,   -- 营业收入 (亿)
    oper_cost             DOUBLE PRECISION,   -- 营业成本 (亿)
    net_income            DOUBLE PRECISION,   -- 净利润 (亿)
    gross_margin          DOUBLE PRECISION,   -- 毛利率 %
    net_margin            DOUBLE PRECISION,   -- 净利率 %
    roe                   DOUBLE PRECISION,   -- ROE %
    or_yoy                DOUBLE PRECISION,   -- 营收同比 %
    netprofit_yoy         DOUBLE PRECISION,   -- 净利同比 %
    sell_exp              DOUBLE PRECISION,   -- 销售费用 (亿)
    admin_exp             DOUBLE PRECISION,   -- 管理费用 (亿)
    fin_exp               DOUBLE PRECISION,   -- 财务费用 (亿)
    rd_exp                DOUBLE PRECISION,   -- 研发费用 (亿)
    total_assets          DOUBLE PRECISION,   -- 总资产 (亿)
    total_cur_assets      DOUBLE PRECISION,   -- 流动资产 (亿)
    money_cap             DOUBLE PRECISION,   -- 货币资金 (亿)
    accounts_receiv       DOUBLE PRECISION,   -- 应收账款 (亿)
    inventory             DOUBLE PRECISION,   -- 存货 (亿)
    fixed_assets          DOUBLE PRECISION,   -- 固定资产 (亿)
    contract_liab         DOUBLE PRECISION,   -- 合同负债 (亿)
    total_liab            DOUBLE PRECISION,   -- 总负债 (亿)
    total_cur_liab        DOUBLE PRECISION,   -- 流动负债 (亿)
    debt_to_assets        DOUBLE PRECISION,   -- 资产负债率 %
    current_ratio         DOUBLE PRECISION,   -- 流动比率
    quick_ratio           DOUBLE PRECISION,   -- 速动比率
    ar_turn               DOUBLE PRECISION,   -- 应收账款周转率 (次)
    inv_turn              DOUBLE PRECISION,   -- 存货周转率 (次)
    assets_turn           DOUBLE PRECISION,   -- 总资产周转率 (次)
    equity_multiplier     DOUBLE PRECISION,   -- 权益乘数
    ocf                   DOUBLE PRECISION,   -- 经营现金流净额 (亿)
    icf                   DOUBLE PRECISION,   -- 投资现金流净额 (亿)
    fcf                   DOUBLE PRECISION,   -- 筹资现金流净额 (亿)
    net_cash_ratio        DOUBLE PRECISION,   -- 净现比 = 经营现金流/净利润
    end_date              VARCHAR(16)  DEFAULT '',
    updated_at            TIMESTAMP    DEFAULT now(),
    UNIQUE (ts_code, year)
);
CREATE INDEX IF NOT EXISTS idx_fd_ind_year ON financial_data (industry, year);
CREATE INDEX IF NOT EXISTS idx_fd_code_year ON financial_data (ts_code, year);
"""

_FIN_COLS = [
    "ts_code", "symbol", "name", "industry", "year",
    "total_revenue", "oper_cost", "net_income", "gross_margin", "net_margin",
    "roe", "or_yoy", "netprofit_yoy", "sell_exp", "admin_exp", "fin_exp", "rd_exp",
    "total_assets", "total_cur_assets", "money_cap", "accounts_receiv", "inventory",
    "fixed_assets", "contract_liab", "total_liab", "total_cur_liab", "debt_to_assets",
    "current_ratio", "quick_ratio", "ar_turn", "inv_turn", "assets_turn",
    "equity_multiplier", "ocf", "icf", "fcf", "net_cash_ratio", "end_date",
]

_FIN_UPSERT_SQL = f"""
INSERT INTO financial_data ({", ".join(_FIN_COLS)})
VALUES ({", ".join("%(" + c + ")s" for c in _FIN_COLS)})
ON CONFLICT (ts_code, year) DO UPDATE SET
{", ".join(f"{c} = EXCLUDED.{c}" for c in _FIN_COLS if c not in ("ts_code", "year"))},
updated_at = now();
"""


def init_financial_schema() -> None:
    """创建 financial_data 表与索引 (幂等)。"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(FINANCIAL_SCHEMA_DDL)
        conn.commit()


def upsert_financial_rows(rows: list[dict]) -> int:
    """按 (ts_code, year) upsert 写入 tushare 财务指标, 返回写入行数。"""
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(_FIN_UPSERT_SQL, {c: r.get(c) for c in _FIN_COLS})
        conn.commit()
    return len(rows)


def get_financial_row(ts_code: str, year: int) -> dict | None:
    """按 ts_code+year 查询单条财务数据 (RealDict), 不存在返回 None。"""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM financial_data WHERE ts_code = %s AND year = %s LIMIT 1;",
                (ts_code, int(year)),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def has_financial(ts_code: str, year: int) -> bool:
    return get_financial_row(ts_code, year) is not None


def count_financial_rows(industry: str = "", year: int | None = None) -> int:
    """统计 financial_data 行数 (可选按行业子串/年份)。"""
    conds: list[str] = []
    params: list = []
    if industry:
        conds.append("industry LIKE %s")
        params.append(f"%{industry}%")
    if year is not None:
        conds.append("year = %s")
        params.append(int(year))
    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM financial_data {where_sql};", params)
            return int(cur.fetchone()[0])
