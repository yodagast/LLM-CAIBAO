"""每日公司推荐扫描脚本: 全市场区间交易参数估算 → 入库 daily_band_recommend → 筛选买入价≥收盘价。

用法:
  python scripts/daily_scan.py                              # 全市场 (约5200只, 耗时数小时, 建议后台运行)
  python scripts/daily_scan.py --limit 50                   # 调试: 只算前 50 只
  python scripts/daily_scan.py --codes 600036,000858
  python scripts/daily_scan.py --industry 白酒
  python scripts/daily_scan.py --objective drawdown --max-trades 50
  python scripts/daily_scan.py --start 2020 --end 2025      # 指定回测起止时间

说明:
  - 复用区间交易参数估算逻辑 (前复权 + T+1 + 剔除无效交易 + 交易次数上限)
  - 结果按 (calc_date, ts_code) 幂等 upsert, 可重复运行续跑
  - 推荐 = 估算买入价 ≥ 当日收盘价 的公司 (可在页面/API 查询)
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import daily_recommend_service as drs  # noqa: E402
from scripts.download_annual_reports import _load_env  # noqa: E402


async def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="每日公司推荐扫描 (区间交易参数估算)")
    parser.add_argument("--codes", default="", help="指定代码(逗号分隔); 空=按行业或全市场")
    parser.add_argument("--industry", default="", help="行业名称(东财分类); 空=全市场")
    parser.add_argument("--limit", type=int, default=0, help="限制计算数量(0=不限, 全市场约5200只)")
    parser.add_argument("--objective", default="balanced", help="优化目标")
    parser.add_argument("--min-sharpe", type=float, default=1.0, help="目标夏普下限")
    parser.add_argument("--max-trades", type=int, default=100, help="交易次数上限(0=不限)")
    parser.add_argument("--start", default="20170101", help="回测起始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="回测结束日期 YYYYMMDD, 空=最新")
    parser.add_argument("--sleep", type=float, default=0.0, help="每只间隔秒数")
    args = parser.parse_args()

    max_trades = args.max_trades if args.max_trades and args.max_trades > 0 else None
    print(f"每日推荐扫描: 目标={args.objective} 夏普≥{args.min_sharpe} "
          f"交易≤{max_trades if max_trades else '不限'} 区间 {args.start}~{args.end or '最新'}")
    t0 = time.time()
    r = await drs.scan_all(codes=args.codes, industry=args.industry, limit=args.limit,
                           objective=args.objective, min_sharpe=args.min_sharpe,
                           max_trades=max_trades, sleep=args.sleep,
                           start_date=args.start, end_date=args.end)
    print(f"\n完成: 扫描 {r['scanned']} 只 (成功 {r['ok']}, 失败 {r['fail']}), "
          f"入库 {r['stored']} 行, 推荐(买入价≥收盘价) {r['recommend_count']} 只, "
          f"计算日 {r['calc_date']}, 耗时 {(time.time() - t0) / 60:.1f} 分钟")
    # 打印推荐前 20
    for it in r["items"][:20]:
        print(f"  {it['name']:<8} {it['ts_code']:<11} 收盘 {it['close']:>8.2f} "
              f"买 {it['buy_price']:>8.2f} 卖 {it['sell_price']:>8.2f} 损 {it['stop_price']:>8.2f} "
              f"收益 {it['total_return']:>7.2f}% 夏普 {it['sharpe']:>5.2f}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
