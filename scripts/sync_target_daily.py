#!/usr/bin/env python3
"""同步「我的股票 ∪ 策略Hub股票」的 A股/ETF + 港股 日线数据到本地 PostgreSQL。

用途:
  - 每天收盘后把自选股与策略Hub中股票的日线持久化到本地库 (stock_daily_bars)
  - A股/ETF 走 tushare (含复权因子/换手率); 港股走腾讯港股 K 线 (kind='hk')
  - 之后再读这些股票的日线时可直接命中本地库, 减少外部接口调用

用法:
    python scripts/sync_target_daily.py                  # 默认最近 10 年, A股+港股
    python scripts/sync_target_daily.py --years 5        # 最近 5 年
    python scripts/sync_target_daily.py --only-a         # 只同步 A股/ETF
    python scripts/sync_target_daily.py --only-hk        # 只同步港股
    LIMIT=20 python scripts/sync_target_daily.py         # 仅处理前 20 只 (测试/续跑)

定时任务 (每天 17:30, 收盘数据落定后):
    30 17 * * * /Users/huangyong/git/LLM-CAIBAO/.venv/bin/python \
        /Users/huangyong/git/LLM-CAIBAO/scripts/sync_target_daily.py \
        >> /Users/huangyong/git/LLM-CAIBAO/logs/sync_target_daily.log 2>&1

依赖: 项目 .venv (tushare token 从根 .env 读取), 本地 PostgreSQL (llm_caibao)。
幂等 upsert, 可重复执行。
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
    """判断 ts_code 类型: hk(港股) / fund(ETF) / stock(A股)。"""
    if str(ts_code).upper().endswith(".HK"):
        return "hk"
    return "fund" if data_service._is_fund_code(str(ts_code)) else "stock"


async def main() -> None:
    parser = argparse.ArgumentParser(description="同步自选股+策略Hub的 A股/港股日线")
    parser.add_argument("--years", type=int, default=int(os.getenv("YEARS", "10")),
                        help="回填年数 (默认 10; 港股受腾讯接口限制实际约 8 年)")
    parser.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "0")),
                        help="最多处理多少只 (0=全部, 测试/续跑用)")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CONCURRENCY", "4")),
                        help="并发上限 (默认 4, 控接口限频)")
    parser.add_argument("--only-a", action="store_true", help="只同步 A股/ETF")
    parser.add_argument("--only-hk", action="store_true", help="只同步港股")
    args = parser.parse_args()

    print("[sync_target_daily] 获取目标列表 (我的股票 ∪ 策略Hub股票)...")
    codes = await pg_service.my_and_strategy_codes()
    if args.limit and args.limit > 0:
        codes = codes[:args.limit]
    a_targets = [{"ts_code": c, "kind": classify(c)} for c in codes if classify(c) != "hk"]
    hk_targets = [{"ts_code": c} for c in codes if classify(c) == "hk"]
    print(f"[sync_target_daily] 目标 {len(codes)} 只 (A股/ETF {len(a_targets)}, 港股 {len(hk_targets)})")

    if not a_targets and not hk_targets:
        print("[sync_target_daily] 目标列表为空 (自选股与策略Hub均无股票), 跳过。")
        return

    failed = 0
    if a_targets and not args.only_hk:
        print(f"[sync_target_daily] 同步 A股/ETF 日线 (最近 {args.years} 年)...")
        res = await data_service.backfill_daily_bars(
            a_targets, years=args.years, concurrency=args.concurrency)
        print(f"[sync_target_daily] A股/ETF 完成: ok={res['ok']} skip={res['skip']} rows={res['rows']}")
        for e in res["errors"][:10]:
            print(f"  !! {e['ts_code']}: {e['msg']}")
        failed += res["skip"]

    if hk_targets and not args.only_a:
        print(f"[sync_target_daily] 同步 港股 日线 (最近 {args.years} 年)...")
        res = await data_service.backfill_hk_daily_bars(
            hk_targets, years=args.years, concurrency=args.concurrency)
        print(f"[sync_target_daily] 港股 完成: ok={res['ok']} skip={res['skip']} rows={res['rows']}")
        for e in res["errors"][:10]:
            print(f"  !! {e['ts_code']}: {e['msg']}")
        failed += res["skip"]

    print(f"[sync_target_daily] 全部完成 (跳过节数: {failed})。")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
