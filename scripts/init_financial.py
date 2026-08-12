"""全量保存 tushare 财务数据到本地 PostgreSQL (financial_data 表)。

用法:
  python scripts/init_financial.py                              # 全市场 2022~2024
  python scripts/init_financial.py --industry 白酒 --start 2020 --end 2024
  python scripts/init_financial.py --codes 600036,000858 --start 2023 --end 2024
  python scripts/init_financial.py --limit 20                   # 调试: 只处理前 20 只

说明:
  - 全市场约 5204 只 × 每只 4~5 次 tushare 调用 (fina_indicator/balancesheet/income/cashflow/daily_basic),
    耗时较长, 建议按行业分批运行
  - 幂等 upsert (按 ts_code+year 唯一), 可重复运行续跑
  - 表: financial_data (ROE/净利率/毛利率/负债率/现金流/净现比/营收/净利/估值等)
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import caibao_service as cs  # noqa: E402
from api import pg_service  # noqa: E402
from scripts.download_annual_reports import _load_env, get_stock_list  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="全量保存 tushare 财务数据到 pgsql (financial_data)")
    parser.add_argument("--codes", default="", help="指定股票代码(逗号分隔); 空=全市场")
    parser.add_argument("--industry", default="", help="行业名称(东财分类), 空=全市场")
    parser.add_argument("--start", type=int, default=2022, help="起始年份")
    parser.add_argument("--end", type=int, default=2024, help="结束年份")
    parser.add_argument("--max-stocks", type=int, default=6000, help="最多扫描股票数")
    parser.add_argument("--limit", type=int, default=0, help="限制处理股票数(调试用, 0=不限)")
    parser.add_argument("--sleep", type=float, default=0.0, help="每只股票间延时秒数")
    args = parser.parse_args()

    _load_env()
    await pg_service.init_financial_schema()
    years = list(range(args.start, args.end + 1))

    # 股票列表
    if args.codes:
        stocks = []
        for c in args.codes.split(","):
            c = c.strip()
            if not c:
                continue
            sym = c.split(".")[0]
            ts_code = c if "." in c else f"{sym}.{'SH' if sym.startswith('6') else 'SZ'}"
            stocks.append((ts_code, sym))
    else:
        stocks = get_stock_list(args.industry)
    if args.limit and args.limit > 0:
        stocks = stocks[: args.limit]

    print(f"开始: {len(stocks)} 只股票, 年份 {years}, 表 financial_data")
    t0 = time.time()
    total_rows = 0
    ok = fail = 0
    for i, (ts_code, name) in enumerate(stocks, 1):
        try:
            n = await cs.sync_stock_financial(ts_code, years)
            if n:
                total_rows += n
                ok += 1
            else:
                fail += 1
            print(f"  [{i}/{len(stocks)}] {name} ({ts_code}) 入库 {n} 行")
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(stocks)}] {name} ({ts_code}) 失败: {e}")
        if args.sleep and args.sleep > 0:
            await asyncio.sleep(args.sleep)

    print(f"\n完成: 成功 {ok} 只, 失败 {fail} 只, 共入库 {total_rows} 行, "
          f"耗时 {(time.time() - t0) / 60:.1f} 分钟")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
