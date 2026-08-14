#!/usr/bin/env python3
"""同步 目标股票列表 的公司大事 (网络搜索 Bing News + DeepSeek 总结) 到本地 pgsql stock_events。

流程:
  1. 获取目标股票列表 (我的股票 ∪ 策略Hub股票 ∪ ETF), 排除基金/港股
  2. 默认只处理尚无大事记录的股票 (跳过已有, 续跑快); --force 全量重新生成
  3. 每只: Bing News RSS 搜索 "<公司名> 大事" → DeepSeek 总结成时间线 JSON → upsert

用法:
    python scripts/sync_stock_events.py                  # 只补缺失
    python scripts/sync_stock_events.py --force          # 全量重新生成
    python scripts/sync_stock_events.py --limit 20       # 本次最多 20 只
    python scripts/sync_stock_events.py --code 600036.SH # 只处理指定股票

依赖: 项目 .venv + 根 .env 的 DEEPSEEK_API_KEY (未配置时跳过 LLM 总结, 只入库搜索片段为空)。
DeepSeek 调用较慢 (每只约 5~20s), 默认并发 2。
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
    parser.add_argument("--force", action="store_true", help="全量重新生成 (默认只补缺失)")
    parser.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "0")),
                        help="本次最多处理多少只 (0=不限)")
    parser.add_argument("--concurrency", type=int, default=2, help="DeepSeek 并发上限 (默认 2)")
    parser.add_argument("--code", default="", help="只处理指定 ts_code (如 600036.SH)")
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

    print(f"[sync_stock_events] 开始同步 (force={args.force}, limit={args.limit or '全部'})...")
    res = await events_service.sync_events_batch(codes, force=args.force,
                                                 limit=args.limit,
                                                 concurrency=args.concurrency)
    print(f"[sync_stock_events] 完成: total={res['total']} ok={res['ok']} "
          f"empty={res['empty']} error={res['error']}")
    for e in res["errors"][:10]:
        print(f"  !! {e['code']}: {e['msg']}")


if __name__ == "__main__":
    asyncio.run(main())
