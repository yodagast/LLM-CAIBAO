"""全市场基本面数据初始化脚本 (ROE 杜邦拆分指标)。

用法:
  python scripts/init_fundamental.py                        # 全市场 2024~2025
  python scripts/init_fundamental.py --industry 白酒 --start 2020 --end 2025
  python scripts/init_fundamental.py --industry 银行 --start 2024 --end 2024 --max-stocks 500

说明:
  - 全市场约 5400 只 × 每只 4 次 tushare 调用, 耗时可能 1~2 小时, 建议按行业分批
  - 幂等 upsert, 可重复运行续跑
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import fundamental_service as fs  # noqa: E402
from api import pg_service  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="基本面数据初始化 (ROE 杜邦拆分)")
    parser.add_argument("--industry", default="", help="行业名称(东财分类), 空=全市场")
    parser.add_argument("--start", type=int, default=2024, help="起始年份")
    parser.add_argument("--end", type=int, default=2025, help="结束年份")
    parser.add_argument("--max-stocks", type=int, default=1000, help="最多扫描股票数(全市场时生效)")
    args = parser.parse_args()

    pg_service.init_fundamental_schema()
    years = list(range(args.start, args.end + 1))
    print(f"初始化: 行业={args.industry or '全市场'} 年份={years} max_stocks={args.max_stocks}")

    t0 = time.time()
    result = fs.sync_industry_years(args.industry, years, max_stocks=args.max_stocks)
    print(f"\n完成: 扫描 {result['scanned_total']} 条记录, 入库 {result['stored_total']} 条, "
          f"耗时 {time.time() - t0:.0f} 秒")
    for y, v in result["per_year"].items():
        print(f"  {y}: scanned={v['scanned']} stored={v['stored']} failed={v['failed']}")


if __name__ == "__main__":
    main()
