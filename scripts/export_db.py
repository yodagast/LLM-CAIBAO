"""导出 PostgreSQL 业务表到 CSV 备份。

用法:
  python scripts/export_db.py                        # 导出全部业务表到 backups/
  python scripts/export_db.py --table red_low_vol    # 只导出指定表 (可多次)
  python scripts/export_db.py --out-dir /path/dir    # 指定输出目录

说明:
  - 默认导出表: red_low_vol, fundamental_screen
  - 排除自增主键 id, 保留业务数据与 updated_at 时间戳
  - 文件名: {表名}_{YYYYMMDD_HHMMSS}.csv (UTF-8, CSV HEADER)
"""
import argparse
import io
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from api import pg_service  # noqa: E402

DEFAULT_TABLES = ["red_low_vol", "fundamental_screen"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def _business_cols(conn, table: str) -> list[str]:
    """获取业务列 (排除自增主键 id), 按表结构顺序。"""
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name <> 'id' ORDER BY ordinal_position;",
        table,
    )
    return [r[0] for r in rows]


async def export_table(conn, table: str, out_dir: Path) -> Path:
    """导出单表为 CSV, 返回文件路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = await _business_cols(conn, table)
    if not cols:
        raise RuntimeError(f"表 {table} 不存在或无业务列")
    col_sql = ", ".join(cols)
    buf = io.BytesIO()
    # asyncpg copy_from_query 会包装成 COPY (<query>) TO STDOUT, 传 SELECT + format 选项即可
    await conn.copy_from_query(f"SELECT {col_sql} FROM {table}", output=buf,
                               format="csv", header=True)
    data = buf.getvalue().decode("utf-8")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{table}_{ts}.csv"
    path.write_text(data, encoding="utf-8")
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(description="导出 PostgreSQL 业务表到 CSV")
    parser.add_argument("--table", action="append", default=[],
                        help="要导出的表 (可多次; 默认导出全部业务表)")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "backups"),
                        help="输出目录 (默认 backups/)")
    args = parser.parse_args()

    tables = args.table or DEFAULT_TABLES
    out_dir = Path(args.out_dir)

    # 确保表结构存在 (导出空表也 OK)
    await pg_service.init_schema()
    await pg_service.init_fundamental_schema()

    pool = await pg_service._get_pool()
    async with pool.acquire() as conn:
        for table in tables:
            path = await export_table(conn, table, out_dir)
            rows = sum(1 for _ in path.open(encoding="utf-8")) - 1  # 减 header
            print(f"导出 {table}: {rows} 行 -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
