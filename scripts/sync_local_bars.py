#!/usr/bin/env python3
"""同步 目标股票列表 (我的股票 ∪ 策略Hub股票 ∪ ETF) 最近 N 年日线 + 财务数据 到本地 pgsql。

用途:
  - 把前端依赖的日线/财务数据持久化到本地 PostgreSQL (stock_daily_bars / financial_data)
  - 之后前端接口 (详情/K线/自选股列表) 优先从本地 pgsql 加载, 不再逐只打 tushare

用法:
    python scripts/sync_local_bars.py                 # 默认**增量** (按本地最新日回看 30 天)
    python scripts/sync_local_bars.py --full           # 强制全量 (最近 10 年)
    YEARS=5 python scripts/sync_local_bars.py --full   # 全量最近 5 年
    LIMIT=50 python scripts/sync_local_bars.py        # 仅处理前 50 只 (测试/续跑)
    python scripts/sync_local_bars.py --only-bars      # 只回填日线, 跳过财务补漏
    python scripts/sync_local_bars.py --only-fin       # 只补漏财务, 跳过日线

增量策略: 已入库的股票只回看 --lookback-days 天 (默认 30) 并幂等 upsert;
本地无数据的股票 (新加自选股/策略Hub) 自动按 years 全量首次入库。

依赖: 项目 .venv (tushare token 从根 .env 读取), 本地 PostgreSQL (llm_caibao)。
幂等 upsert, 可重复执行; 受 tushare 限频。
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
    parser = argparse.ArgumentParser(description="同步目标列表日线/财务到本地 pgsql")
    parser.add_argument("--years", type=int, default=int(os.getenv("YEARS", "10")),
                        help="回填年数 (默认 10)")
    parser.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "0")),
                        help="最多处理多少只 (0=全部, 测试/续跑用)")
    parser.add_argument("--only-bars", action="store_true", help="只回填日线")
    parser.add_argument("--only-fin", action="store_true", help="只补漏财务")
    parser.add_argument("--concurrency", type=int, default=4, help="tushare 并发上限 (默认 4)")
    parser.add_argument("--full", action="store_true",
                        help="强制全量拉取 (默认增量: 按本地最新交易日回看 lookback 天)")
    parser.add_argument("--lookback-days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "30")),
                        help="增量回看自然日数 (默认 30, 覆盖长假)")
    args = parser.parse_args()

    years = args.years
    incremental = not args.full

    print(f"[sync_local_bars] 获取目标列表 (我的股票 ∪ 策略Hub股票 ∪ ETF)...")
    codes = await pg_service.target_sync_codes()
    print(f"[sync_local_bars] 目标 {len(codes)} 只")
    if args.limit and args.limit > 0:
        codes = codes[:args.limit]
    # 港股走腾讯行情 (kind='hk'), A股/ETF 走 tushare
    a_codes = [c for c in codes if not str(c).upper().endswith(".HK")]
    hk_codes = [c for c in codes if str(c).upper().endswith(".HK")]
    targets = [{"ts_code": c, "kind": classify(c)} for c in a_codes]
    mode = f"增量 (回看 {args.lookback_days} 天)" if incremental else f"全量 ({years} 年)"
    print(f"[sync_local_bars] A股/ETF {len(targets)} 只, 港股 {len(hk_codes)} 只; 模式: {mode}")

    if not args.only_fin and targets:
        print(f"[sync_local_bars] 回填 A股/ETF 日线 ({mode})...")
        res = await data_service.backfill_daily_bars(targets, years=years,
                                                     concurrency=args.concurrency,
                                                     incremental=incremental,
                                                     lookback_days=args.lookback_days)
        print(f"[sync_local_bars] A股/ETF 日线回填完成: ok={res['ok']} skip={res['skip']} rows={res['rows']}")
        for e in res["errors"][:10]:
            print(f"  !! {e['ts_code']}: {e['msg']}")

    if not args.only_fin and hk_codes:
        print(f"[sync_local_bars] 回填 港股 日线 ({mode})...")
        res_hk = await data_service.backfill_hk_daily_bars(
            [{"ts_code": c} for c in hk_codes], years=years,
            concurrency=args.concurrency,
            incremental=incremental, lookback_days=args.lookback_days)
        print(f"[sync_local_bars] 港股 日线回填完成: ok={res_hk['ok']} skip={res_hk['skip']} rows={res_hk['rows']}")
        for e in res_hk["errors"][:10]:
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
