"""从 CSV 备份导入 PostgreSQL 业务表 (幂等 upsert)。

用法:
  python scripts/import_db.py --file backups/red_low_vol_20260803_1720.csv
  python scripts/import_db.py --dir backups                    # 导入目录下所有备份
  python scripts/import_db.py --dir backups --table red_low_vol  # 只导入指定表

说明:
  - 从文件名前缀推断表名 (red_low_vol_*.csv -> red_low_vol)
  - 按业务唯一键 (ts_code, year) 幂等 upsert, 可重复导入
  - 需保证 CSV 由 export_db.py 生成 (业务列, 含表头)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import pg_service  # noqa: E402

# 业务唯一键 (upsert 冲突键)
UNIQUE_KEYS = {
    "red_low_vol": ["ts_code", "year"],
    "fundamental_screen": ["ts_code", "year"],
}


def _ensure_table(table: str) -> None:
    if table == "red_low_vol":
        pg_service.init_schema()
    else:
        pg_service.init_fundamental_schema()


def _business_cols(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name <> 'id' ORDER BY ordinal_position;",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def import_table(table: str, filepath: Path) -> int:
    """导入单表 CSV, 返回写入/更新行数。"""
    if table not in UNIQUE_KEYS:
        raise RuntimeError(f"不支持的表: {table}, 可用: {list(UNIQUE_KEYS)}")
    _ensure_table(table)

    with pg_service._connect() as conn:
        with conn.cursor() as cur:
            cols = _business_cols(cur, table)
            col_sql = ", ".join(cols)
            tmp = f"tmp_{table}"
            cur.execute(f"DROP TABLE IF EXISTS {tmp}")
            cur.execute(f"CREATE TEMP TABLE {tmp} (LIKE {table} EXCLUDING IDENTITY)")
            cur.execute(f"ALTER TABLE {tmp} DROP COLUMN IF EXISTS id")
            with open(filepath, "r", encoding="utf-8") as f:
                cur.copy_expert(f"COPY {tmp} ({col_sql}) FROM STDIN WITH (FORMAT csv, HEADER true)", f)

            unique = UNIQUE_KEYS[table]
            upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols
                            if c not in unique and c != "updated_at")
            cur.execute(
                f"INSERT INTO {table} ({col_sql}) SELECT {col_sql} FROM {tmp} "
                f"ON CONFLICT ({', '.join(unique)}) DO UPDATE SET {upd}, updated_at = now()",
            )
            n = cur.rowcount
        conn.commit()
    return n


def table_from_filename(name: str) -> str | None:
    for t in UNIQUE_KEYS:
        if name.startswith(t + "_"):
            return t
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="从 CSV 导入 PostgreSQL 业务表")
    parser.add_argument("--file", default="", help="单个 CSV 文件路径")
    parser.add_argument("--dir", default="", help="备份目录, 导入其中所有 CSV")
    parser.add_argument("--table", default="", help="限定表名 (与 --dir 配合)")
    args = parser.parse_args()

    if not args.file and not args.dir:
        parser.error("请提供 --file 或 --dir")

    files: list[Path] = []
    if args.file:
        files = [Path(args.file)]
    else:
        files = sorted(Path(args.dir).glob("*.csv"))

    if not files:
        print("未找到可导入的 CSV 文件")
        return

    total = 0
    for path in files:
        table = args.table or table_from_filename(path.name)
        if not table:
            print(f"跳过 {path.name}: 无法识别表名 (可用 --table 指定)")
            continue
        n = import_table(table, path)
        print(f"导入 {path.name} -> {table}: {n} 行 (upsert)")
        total += n
    print(f"完成, 共处理 {total} 行")


if __name__ == "__main__":
    main()
