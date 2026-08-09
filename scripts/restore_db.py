"""从 .sql 备份导入 llm_caibao 数据库 (scripts/dump_db.py 生成的 pg_dump plain 风格文件)。

用法:
  python scripts/restore_db.py --file backups/llm_caibao_20260807_120000.sql
  python scripts/restore_db.py --file dump.sql --connect postgresql://user:pass@host:5432/db
  python scripts/restore_db.py --file dump.sql --no-drop    # 保留已有表 (表结构需兼容)

说明:
  - 解析并执行 dump_db.py 生成的 .sql: CREATE TABLE / 索引 / COPY 数据块 / setval / 外键
  - 默认先 DROP 再重建 dump 中出现的表, 可重复导入, 目标库无残留旧表问题
  - --no-drop: 已存在的表跳过 CREATE (数据仍导入), 适合表结构已就绪的场景
  - --data-only 导出的文件不含 CREATE TABLE, 直接导入数据即可
  - 连接串默认取根目录 .env 的 DATABASE_URL, 用 --connect 可导入到其他机器/实例
  - 事务边界沿用 dump 文件内的 BEGIN/COMMIT (psql -f 同款行为)
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import pg_service  # noqa: E402

# CREATE TABLE public.xxx (
_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?([A-Za-z_][\w$]*)\s*\("
)
_STMT_END = (";",)


def _collect_create_tables(path: Path) -> list[str]:
    """第一遍扫描: 收集文件里出现的 CREATE TABLE 表名 (含 COPY 数据块需跳过)。"""
    tables: list[str] = []
    in_copy = False
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if in_copy:
                if s == "\\.":
                    in_copy = False
                continue
            if s.startswith("COPY ") and " FROM stdin" in s:
                in_copy = True
                continue
            m = _CREATE_TABLE_RE.search(line)
            if m:
                tables.append(m.group(1))
    return tables


def _exec_create_table(cur, stmt: str, keep_existing: bool) -> None:
    """执行完整 CREATE TABLE 语句 (--no-drop 且表已存在时跳过)。"""
    m = _CREATE_TABLE_RE.search(stmt)
    table = m.group(1)
    if keep_existing:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s;",
            (table,),
        )
        if cur.fetchone():
            print(f"跳过已存在的表: {table} (--no-drop)")
            return
    cur.execute(stmt)
    print(f"已创建表: {table}")


def _exec_index(cur, stmt: str) -> None:
    """CREATE INDEX 幂等化 (IF NOT EXISTS)。"""
    if stmt.startswith("CREATE INDEX"):
        stmt = re.sub(r"^CREATE (UNIQUE )?INDEX", r"CREATE \1INDEX IF NOT EXISTS", stmt)
    cur.execute(stmt)


def _restore(conn, path: Path, keep_existing: bool) -> None:
    create_tables = _collect_create_tables(path)
    conn.autocommit = True

    with conn.cursor() as cur:
        if not keep_existing and create_tables:
            for t in create_tables:
                cur.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")
            print(f"已删除 {len(create_tables)} 张旧表 (drop-first)")

        stmt_buf: list[str] = []
        in_copy = False
        copy_header = ""
        copy_lines: list[str] = []
        executed = 0

        def flush_stmt() -> None:
            nonlocal executed
            stmt = "".join(stmt_buf).strip()
            if not stmt:
                return
            if stmt.startswith("CREATE TABLE"):
                if _CREATE_TABLE_RE.search(stmt):
                    _exec_create_table(cur, stmt, keep_existing)
                    executed += 1
                    return
            if stmt.startswith("CREATE INDEX"):
                _exec_index(cur, stmt)
                executed += 1
                return
            # BEGIN / COMMIT / SELECT setval / ALTER TABLE 外键 / COMMENT 等
            cur.execute(stmt)
            executed += 1

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if in_copy:
                    if s == "\\.":
                        # 结束 COPY 块: 执行 COPY ... FROM stdin
                        cur.copy_expert(copy_header, io.StringIO("".join(copy_lines)))
                        executed += 1
                        in_copy = False
                        copy_lines = []
                    else:
                        copy_lines.append(line)
                    continue
                if s.startswith("COPY ") and " FROM stdin" in s:
                    flush_stmt()  # 先执行 COPY 前的语句 (CREATE TABLE)
                    stmt_buf = []
                    in_copy = True
                    copy_header = s.rstrip(";")
                    continue
                stmt_buf.append(line)
                if s.endswith(_STMT_END):
                    flush_stmt()
                    stmt_buf = []
            flush_stmt()  # 文件尾残留语句

        print(f"完成: 共执行 {executed} 条语句 (含 COPY 数据块)")


def main() -> None:
    parser = argparse.ArgumentParser(description="从 .sql 备份导入 llm_caibao 数据库")
    parser.add_argument("--file", required=True, help="dump_db.py 生成的 .sql 文件")
    parser.add_argument("--connect", default="", help="目标连接串 (默认取 .env DATABASE_URL)")
    parser.add_argument("--no-drop", action="store_true", help="不删除已有表 (跳过已存在表的 CREATE)")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        parser.error(f"文件不存在: {path}")

    dsn = args.connect or pg_service._dsn()
    print(f"目标数据库: {dsn}")
    with psycopg2.connect(dsn) as conn:
        _restore(conn, path, keep_existing=args.no_drop)
        print(f"导入完成: {path.name}")


if __name__ == "__main__":
    main()
