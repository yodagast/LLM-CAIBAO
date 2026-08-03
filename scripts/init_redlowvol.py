"""全市场红利低波数据初始化脚本。

用法:
  python scripts/init_redlowvol.py  --max-stocks 1500                      # 全市场 2024~2025
  python scripts/init_redlowvol.py --industry 白酒 --start 2020 --end 2025
  python scripts/init_redlowvol.py --industry 煤炭 --start 2024 --end 2024 --max-stocks 500

说明:
  - 每只股票约 5 次 tushare 调用 (daily/daily_basic/fina_indicator/dividend),
    全市场 5400 只 × 多年份预计 2~3 小时, 建议按行业分批运行
  - 幂等 upsert (按 ts_code+year 唯一), 可重复运行续跑
  - 表格: red_low_vol (股息率/波动率/每股分红/自由现金流/ROE/负债率等)
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import pg_service  # noqa: E402
from api import redlowvol_service as rlv  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="红利低波数据初始化")
    parser.add_argument("--industry", default="", help="行业名称(东财分类), 空=全市场")
    parser.add_argument("--start", type=int, default=2024, help="起始年份")
    parser.add_argument("--end", type=int, default=2025, help="结束年份")
    parser.add_argument("--max-stocks", type=int, default=1000, help="最多扫描股票数(全市场时生效)")
    parser.add_argument("--sleep", type=float, default=0.1, help="每次 tushare 调用的间隔秒数")
    args = parser.parse_args()

    # 确保 red_low_vol 表存在
    pg_service.init_schema()
    years = list(range(args.start, args.end + 1))
    print(f"初始化: 行业={args.industry or '全市场'} 年份={years} "
          f"max_stocks={args.max_stocks} sleep={args.sleep}")

    t0 = time.time()
    result = rlv.sync_industry_years(args.industry, years,
                                     max_stocks=args.max_stocks, sleep=args.sleep)
    print(f"\n完成: 扫描 {result['scanned_total']} 条记录, 入库 {result['stored_total']} 条, "
          f"耗时 {time.time() - t0:.0f} 秒")
    for y, v in result["per_year"].items():
        print(f"  {y}: scanned={v['scanned']} stored={v['stored']} failed={v['failed']}")


if __name__ == "__main__":
    main()
