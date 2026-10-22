"""申万一级行业指数 (31 个) 统计入库脚本。

计算 31 个申万一级行业指数的 涨跌 / 成交额 / 成交量 / 52周高低 / 分位数,
并 upsert 到 PostgreSQL 表 sw_index_screen (幂等, 可断点续跑)。

数据源:
  - 申万宏源官网指数发布接口 (最新交易日快照: 开/高/低/收/成交额/成交量/涨跌)
  - tushare index_classify (31 个申万一级行业) + index_weekly (52 周高低)

用法:
    python scripts/sync_sw_index.py
    python scripts/sync_sw_index.py --sort-by amount --order desc
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import pg_service, sw_index_service  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="申万一级行业指数统计入库")
    parser.add_argument("--sort-by", default="pct_chg", help="排序字段 (仅影响返回顺序)")
    parser.add_argument("--order", default="desc", help="排序方向 asc/desc")
    args = parser.parse_args()

    print("== 申万一级行业指数统计入库 ==")
    result = await sw_index_service.sync_sw_indexes_to_db()
    print(f"  行业数: {result['total']}")
    print(f"  已入库: {result['stored']}")
    print(f"  数据日: {result['calc_date']}")
    print(f"  快照行: {result['snapshot_rows']}")

    data = await sw_index_service.get_sw_indexes(args.sort_by, args.order)
    print(f"\n读取验证: {data['count']} 条 (source={data.get('source')}, 计算日={data.get('calc_date')})")
    for it in data["items"][:5]:
        pct = f"{it['pct_chg']:+.2f}%" if it.get("pct_chg") is not None else "—"
        pos = f"{it['pos52']:.3f}" if it.get("pos52") is not None else "—"
        amt = f"{it['amount']:.1f}" if it.get("amount") is not None else "—"
        print(f"  {it['code']} {it['name']:<6} 涨跌 {pct:>8}  成交额 {amt:>8}亿  分位 {pos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))