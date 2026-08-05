#!/usr/bin/env python3
"""全市场每日推荐扫描脚本: 计算沪深两市全部 A 股 → 入库 daily_band_recommend (支持断点续跑)。

用法:
  python scripts/scan_all_market.py                            # 全市场沪深A股 (~5200只, 数小时)
  python scripts/scan_all_market.py --force                    # 强制全量重算 (不跳过已入库)
  python scripts/scan_all_market.py --objective sharpe --max-trades 50
  python scripts/scan_all_market.py --start 2020 --end 2025    # 指定回测起止时间
  python scripts/scan_all_market.py --batch 100 --sleep 0.2    # 每100只分批入库 + 间隔
  python scripts/scan_all_market.py --limit 10                 # 调试: 只算前 10 只
  nohup python scripts/scan_all_market.py > /tmp/scan_all.log 2>&1 &   # 后台运行

说明:
  - 覆盖沪深两市所有 A 股 (ts_code 以 .SH/.SZ 结尾, 来自 tushare stock_basic list_status=L)
  - 复用区间交易参数估算 (前复权 + T+1 + 剔除无效交易 + max_trades 限制)
  - 结果按 (calc_date, ts_code) 幂等 upsert 分块入库
  - 默认断点续跑: 跳过该计算日已入库的标的, 中断后重跑会自动跳过已算部分
  - 入库含 industry 字段 (东财行业), 便于按行业隔离查询
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import daily_recommend_service as drs  # noqa: E402
from scripts.download_annual_reports import _load_env  # noqa: E402


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="全市场每日推荐扫描 (沪深全部A股 → pgsql)")
    parser.add_argument("--objective", default="balanced",
                        help="优化目标: balanced/return/annual/sharpe/drawdown/calmar")
    parser.add_argument("--min-sharpe", type=float, default=1.0, help="目标夏普下限")
    parser.add_argument("--max-trades", type=int, default=100, help="交易次数上限(0=不限)")
    parser.add_argument("--start", default="20170101", help="回测起始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="回测结束日期 YYYYMMDD, 空=最新")
    parser.add_argument("--batch", type=int, default=200, help="每 N 只分批入库 (默认200)")
    parser.add_argument("--limit", type=int, default=0, help="限制计算数量(0=全市场约5200只; 用于调试)")
    parser.add_argument("--sleep", type=float, default=0.0, help="每只标的间隔秒数 (防限频)")
    parser.add_argument("--force", action="store_true", help="强制全量重算 (默认续跑跳过已入库)")
    args = parser.parse_args()

    max_trades = args.max_trades if args.max_trades and args.max_trades > 0 else None
    total = len(drs._all_stocks(0))
    print(f"全市场扫描: 沪深A股约 {total} 只, 目标={args.objective} 夏普≥{args.min_sharpe} "
          f"交易≤{max_trades if max_trades else '不限'} 区间 {args.start}~{args.end or '最新'} "
          f"批次={args.batch} {'(强制全量重算)' if args.force else '(续跑: 跳过已入库)'}")
    t0 = time.time()
    r = drs.scan_all(limit=args.limit, objective=args.objective, min_sharpe=args.min_sharpe,
                     max_trades=max_trades, sleep=args.sleep,
                     start_date=args.start, end_date=args.end,
                     skip_existing=not args.force, batch=args.batch)
    dt = (time.time() - t0) / 60
    print(f"\n完成: 待扫描 {r['scanned']} 只 (成功 {r['ok']}, 失败 {r['fail']}, 续跑跳过 {r.get('skipped', 0)}), "
          f"入库 {r['stored']} 行, 推荐(买入价≥收盘价) {r['recommend_count']} 只, "
          f"计算日 {r['calc_date']}, 耗时 {dt:.1f} 分钟")
    # 打印推荐前 20
    for it in r["items"][:20]:
        print(f"  {it['name']:<8} {it['ts_code']:<11} [{it['industry'] or '?':<4}] "
              f"收盘 {it['close']:>8.2f} 买 {it['buy_price']:>8.2f} 卖 {it['sell_price']:>8.2f} "
              f"损 {it['stop_price']:>8.2f} 收益 {it['total_return']:>7.2f}% 夏普 {it['sharpe']:>5.2f}")


if __name__ == "__main__":
    main()
