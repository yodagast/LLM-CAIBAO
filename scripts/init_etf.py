"""全市场 ETF 筛选数据初始化脚本 (→ etf_screen 表)。

用法:
  python scripts/init_etf.py                          # 全部已上市 ETF (约 2700 只)
  python scripts/init_etf.py --limit 500              # 仅计算前 500 只 (按规模代理排序)
  python scripts/init_etf.py --refresh                # 忽略 15 分钟缓存, 重新抓取
  python scripts/init_etf.py --sleep 0.1 --batch 200  # 限频 + 每 200 只入库一次

说明:
  - 计算 费用/规模/流动性/折溢价/跟踪偏离/52周高低与位置 等关键指标,
    写入 PostgreSQL 表 etf_screen (按 ts_code 唯一, 幂等 upsert, 可重复运行续跑)
  - 前端 ETF 筛选 tab 检测到 DB 有数据后改为读库 (不再逐只实时计算)
  - 每只 ETF 约 2 次 tushare 调用 (fund_daily + fund_nav), 全市场约 2700 只
    预计 20~60 分钟; 建议由 nightly_update_linux.sh 每日自动运行
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import etf_service  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="ETF 筛选数据初始化 (etf_screen 表)")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多计算数量 (0=全部已上市 ETF, 约2700只)")
    parser.add_argument("--refresh", action="store_true",
                        help="忽略 15 分钟缓存, 重新抓取 tushare")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="每次 tushare 调用的间隔秒数 (默认0)")
    parser.add_argument("--batch", type=int, default=100,
                        help="每多少只入库一次 (默认100, 中断不丢已算数据)")
    args = parser.parse_args()

    print(f"初始化 ETF 筛选数据: limit={args.limit or '全部'} "
          f"refresh={args.refresh} sleep={args.sleep} batch={args.batch}")
    t0 = time.time()
    r = etf_service.sync_all(limit=args.limit, refresh=args.refresh,
                             sleep=args.sleep, batch=args.batch)
    print(f"\n完成: 总数 {r['total']} 只, 成功 {r['ok']} 只, 失败 {r['fail']} 只, "
          f"入库 {r['stored']} 行, 计算日 {r['calc_date'] or '—'}, "
          f"耗时 {time.time() - t0:.0f} 秒")


if __name__ == "__main__":
    main()
