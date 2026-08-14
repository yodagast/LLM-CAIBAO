#!/usr/bin/env python3
"""同步 目标股票列表 (我的股票 ∪ 策略Hub股票 ∪ ETF) 最近 N 年日线 + 财务数据 到本地 pgsql。

用途:
  - 把前端依赖的日线/财务数据持久化到本地 PostgreSQL (stock_daily_bars / financial_data)
  - 之后前端接口 (详情/K线/自选股列表) 优先从本地 pgsql 加载, 不再逐只打 tushare

用法:
    python scripts/sync_local_bars.py                 # 默认最近 10 年, 全量目标列表
    YEARS=5 python scripts/sync_local_bars.py         # 最近 5 年
    LIMIT=50 python scripts/sync_local_bars.py        # 仅处理前 50 只 (测试/续跑)
    python scripts/sync_local_bars.py --only-bars     # 只回填日线, 跳过财务补漏
    python scripts/sync_local_bars.py --only-fin      # 只补漏财务, 跳过日线

依赖: 项目 .venv (tushare token 从根 .env 读取), 本地 PostgreSQL (llm_caibao)。
幂等 upsert, 可重复执行; 受 tushare 限频, 全量约 N 只 × 2 次调用。
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api import data_service, pg_service  # noqa: E402


def classify(ts_code: str) -> str:
    return "fund" if data_service._is_fund_code(str(ts_code)) else "stock"


async def main() -> None:
    parser = argparse.ArgumentParser(description="同步目标列表日线/财务到本地 pgsql")
    parser.add_argument("--years", type=int, default=int(os.getenv("YEARS", "10")),
                        help="回填年数 (默认 10)")
    parser.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "0")),
                        help="最多处理多少只 (0=全部, 测试/续跑用)")
    parser.add_argument("--only-bars", action="store_true", help="只回填日线")
    parser.add_argument("--only-fin", action="store_true", help="只补漏财务")
    parser.add_argument("--concurrency", type=int, default=4, help="tushare 并发上限 (默认 4)")
    args = parser.parse_args()

    print(f"[sync_local_bars] 获取目标列表 (我的股票 ∪ 策略Hub股票 ∪ ETF)...")
    codes = await pg_service.target_sync_codes()
    print(f"[sync_local_bars] 目标 {len(codes)} 只")
    if args.limit and args.limit > 0:
        codes = codes[:args.limit]
    targets = [{"ts_code": c, "kind": classify(c)} for c in codes if not c.endswith(".HK")]
    print(f"[sync_local_bars] 待处理 {len(targets)} 只 (排除港股)")

    if not args.only_fin:
        print(f"[sync_local_bars] 回填日线 (最近 {args.years} 年)...")
        res = await data_service.backfill_daily_bars(targets, years=args.years,
                                                     concurrency=args.concurrency)
        print(f"[sync_local_bars] 日线回填完成: ok={res['ok']} skip={res['skip']} rows={res['rows']}")
        for e in res["errors"][:10]:
            print(f"  !! {e['ts_code']}: {e['msg']}")

    if not args.only_bars:
        print("[sync_local_bars] 补漏财务数据 (financial_data, 仅缺失的 A股)...")
        fin = await data_service.backfill_missing_financial(targets)
        print(f"[sync_local_bars] 财务补漏完成: ok={fin['ok']} skip={fin['skip']}")
        for e in fin["errors"][:10]:
            print(f"  !! {e['ts_code']}: {e['msg']}")

    print("[sync_local_bars] 全部完成。")


if __name__ == "__main__":
    asyncio.run(main())
