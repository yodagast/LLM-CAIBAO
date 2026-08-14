#!/usr/bin/env python3
"""同步 目标股票列表 的公司大事 (网络搜索 DuckDuckGo/Bing + DeepSeek 分批总结) 到本地 pgsql stock_events。

流程:
  1. 获取目标股票列表 (我的股票 ∪ 策略Hub股票 ∪ ETF), 排除基金/港股
  2. 默认按 增量/月度 策略选择需更新的股票 (见 stock_events_update_candidates):
       - 无大事 / 不足 20 件  → 增量更新 (每日)
       - 达到 20 件           → 仅当上次更新超 30 天 → 月度更新
     --force 则全量重新生成
  3. 每只: 网络搜索 "<公司名> 大事" → DeepSeek 分批总结 → 基于已有标题去重增量入库

用法:
    python scripts/sync_stock_events.py                    # 增量+月度策略 (默认)
    python scripts/sync_stock_events.py --force            # 全量重新生成
    python scripts/sync_stock_events.py --limit 20         # 本次最多 20 只
    python scripts/sync_stock_events.py --code 600036.SH   # 只处理指定股票
    python scripts/sync_stock_events.py --min-count 30     # 阈值改为 30 件
    python scripts/sync_stock_events.py --monthly-days 15  # 月度周期改为 15 天

依赖: 项目 .venv + 根 .env 的 DEEPSEEK_API_KEY (未配置时 LLM 生成失败)。
DeepSeek 调用较慢 (每只约 1~3 分钟, 推理模型), 默认并发 2。
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api import data_service, events_service, pg_service  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="同步目标股票列表的公司大事到 pgsql")
    parser.add_argument("--force", action="store_true", help="全量重新生成 (默认增量/月度策略)")
    parser.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "0")),
                        help="本次最多处理多少只 (0=不限)")
    parser.add_argument("--concurrency", type=int, default=2, help="DeepSeek 并发上限 (默认 2)")
    parser.add_argument("--code", default="", help="只处理指定 ts_code (如 600036.SH)")
    parser.add_argument("--min-count", type=int, default=20,
                        help="大事条数阈值: 不足则增量更新 (默认 20)")
    parser.add_argument("--monthly-days", type=int, default=30,
                        help="达到阈值后的月度更新周期天数 (默认 30)")
    args = parser.parse_args()

    if args.code:
        codes = [args.code.strip().upper()]
    else:
        print("[sync_stock_events] 获取目标股票列表...")
        codes = await pg_service.target_sync_codes()
        # 排除基金/港股 (公司大事针对 A股)
        codes = [c for c in codes
                 if not c.endswith(".HK") and not data_service._is_fund_code(str(c))]
        print(f"[sync_stock_events] 目标 {len(codes)} 只 (A股)")

    if args.force:
        print("[sync_stock_events] --force 全量重新生成...")
        res = await events_service.sync_events_batch(codes, force=True,
                                                     limit=args.limit,
                                                     concurrency=args.concurrency)
    else:
        print(f"[sync_stock_events] 按增量/月度策略筛选 (阈值 <{args.min_count}件 增量, "
              f"≥{args.min_count}件且超 {args.monthly_days}天 月度)...")
        todo = await pg_service.stock_events_update_candidates(
            codes, min_count=args.min_count, monthly_days=args.monthly_days)
        if args.limit and args.limit > 0:
            todo = todo[:args.limit]
        print(f"[sync_stock_events] 需更新 {len(todo)} 只 (跳过 {len(codes) - len(todo)} 只: "
              f"已足够且近期更新过)")
        res = await events_service.sync_events_update_batch(todo, concurrency=args.concurrency)

    print(f"[sync_stock_events] 完成: total={res['total']} ok={res['ok']} "
          f"empty={res['empty']} error={res['error']}")
    for e in res["errors"][:10]:
        print(f"  !! {e['code']}: {e['msg']}")


if __name__ == "__main__":
    asyncio.run(main())
