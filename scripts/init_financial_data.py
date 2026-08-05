"""全量保存 tushare 财务数据到本地 PostgreSQL (financial_data 表)。

用途: 把全市场 A 股的 tushare 年度财务指标 (营收/净利/ROE/负债率/现金流/净现比等)
批量保存到本地 PG 表 financial_data, 供财报分析 tab 快速读取, 避免重复调用 tushare。

用法:
  python scripts/init_financial_data.py                        # 全市场 2024~2025
  python scripts/init_financial_data.py --industry 白酒 --start 2020 --end 2025
  python scripts/init_financial_data.py --codes 600036,000858 --start 2023 --end 2024
  python scripts/init_financial_data.py --limit 100            # 只处理前100只(测试)

说明:
  - 全市场约 5200 只 × 每只 4 次 tushare 调用 (fina_indicator/balancesheet/income/cashflow),
    按行业分批运行更稳妥
  - 幂等 upsert (按 ts_code+year 唯一); 已存在于 PG 的年份自动跳过, 可中断续跑
  - 表: financial_data (见 api/pg_service.py)
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import caibao_service  # noqa: E402
from api import pg_service  # noqa: E402
from api import data_service  # noqa: E402


def get_stocks(industry: str = "", max_stocks: int = 6000, codes: list | None = None) -> list[dict]:
    """获取待处理的股票列表 [{ts_code, name, industry}]。"""
    if codes:
        pro = data_service._init_pro()
        try:
            df = pro.stock_basic(list_status="L", fields="ts_code,name,industry")
            name_map = {str(r["ts_code"]): r for _, r in df.iterrows()}
        except Exception:
            name_map = {}
        stocks = []
        for c in codes:
            c = c.strip()
            if not c:
                continue
            sym = c.split(".")[0]
            tc = c if "." in c else f"{sym}.{'SH' if sym.startswith('6') else 'SZ'}"
            row = name_map.get(tc, {})
            stocks.append({"ts_code": tc,
                           "name": str(row.get("name", sym)),
                           "industry": str(row.get("industry", "") or "")})
        return stocks

    stocks = []
    for st in data_service._stock_basic().to_dict("records"):
        if industry and industry not in str(st.get("industry", "")):
            continue
        stocks.append({"ts_code": st["ts_code"],
                       "name": str(st.get("name", "")),
                       "industry": str(st.get("industry", "") or "")})
        if max_stocks and len(stocks) >= max_stocks:
            break
    return stocks


def sync_stock(stock: dict, years: list[int], sleep: float) -> tuple[int, int]:
    """处理单只股票: 拉取缺失年份财务数据并保存到 PG。返回 (stored, failed)。"""
    ts_code = stock["ts_code"]
    missing = [y for y in years if not pg_service.has_financial(ts_code, y)]
    if not missing:
        return 0, 0
    try:
        fin = caibao_service.collect_financials(ts_code, missing)
        rows = caibao_service.financials_to_rows(fin, ts_code, stock)
        stored = pg_service.upsert_financial_rows(rows)
        time.sleep(sleep)
        return stored, 0
    except Exception as e:
        print(f"    ! {ts_code} 失败: {e}")
        return 0, 1


def main() -> None:
    parser = argparse.ArgumentParser(description="全量保存 tushare 财务数据到本地 PG (financial_data)")
    parser.add_argument("--industry", default="", help="行业名称(东财分类), 空=全市场")
    parser.add_argument("--start", type=int, default=2024, help="起始年份")
    parser.add_argument("--end", type=int, default=2025, help="结束年份")
    parser.add_argument("--max-stocks", type=int, default=6000, help="最多处理股票数(默认6000覆盖全市场)")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只(测试用, 0=不限)")
    parser.add_argument("--codes", default="", help="指定股票代码, 逗号分隔 (如 600036,000858)")
    parser.add_argument("--sleep", type=float, default=0.05, help="每只股票间的 tushare 调用间隔秒数")
    args = parser.parse_args()

    pg_service.init_financial_schema()
    years = list(range(args.start, args.end + 1))
    codes = [c for c in args.codes.split(",") if c.strip()] if args.codes else None

    stocks = get_stocks(args.industry, args.max_stocks, codes)
    if args.limit and args.limit > 0:
        stocks = stocks[: args.limit]

    print(f"初始化财务数据: 股票 {len(stocks)} 只, 年份 {years}, "
          f"行业={args.industry or '全市场'}, sleep={args.sleep}s")

    t0 = time.time()
    total_stored = total_skipped = total_failed = 0
    for i, stock in enumerate(stocks, 1):
        name = stock["name"] or stock["ts_code"]
        stored, failed = sync_stock(stock, years, args.sleep)
        if stored == 0 and failed == 0:
            total_skipped += 1
            print(f"  [{i}/{len(stocks)}] {name} ({stock['ts_code']}) 已在库, 跳过")
        else:
            total_stored += stored
            total_failed += failed
            print(f"  [{i}/{len(stocks)}] {name} ({stock['ts_code']}) 保存 {stored} 条"
                  + (f" 失败 {failed}" if failed else ""))
        if i % 50 == 0:
            print(f"  ... 进度 {i}/{len(stocks)}, 累计新增 {total_stored} 条")

    print(f"\n完成: 新增入库 {total_stored} 条 | 已存在跳过 {total_skipped} 只 | 失败 {total_failed} 条 | "
          f"耗时 {(time.time() - t0) / 60:.1f} 分钟")
    print(f"financial_data 表当前总行数: {pg_service.count_financial_rows()}")


if __name__ == "__main__":
    main()
