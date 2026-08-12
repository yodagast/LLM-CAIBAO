"""港股红利低波数据初始化脚本。

用法:
  python scripts/init_hk_redlowvol.py                               # 全市场 2024~2025
  python scripts/init_hk_redlowvol.py --industry 银行 --start 2020 --end 2025
  python scripts/init_hk_redlowvol.py --codes 00700,00005 --years 2025   # 指定股票调试
  python scripts/init_hk_redlowvol.py --build-industry                   # 仅重建行业映射缓存

说明:
  - 数据源: 东财港股财务/分红 + 腾讯港股日线 + tushare hk_basic (股票列表)
  - 每只股票约 3 次东财调用 (财务指标/分红/资产负债表) + 1 次腾讯K线; 全市场约 2782 只, 建议按行业分批
  - 幂等 upsert (按 ts_code+year 唯一), 可重复运行续跑
  - 表格: hk_red_low_vol (股息率/波动率/每股分红/自由现金流/ROE/负债率等, 金额单位为万港元)
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import hk_data_service as hkd  # noqa: E402
from api import hk_redlowvol_service as hkrlv  # noqa: E402
from api import pg_service  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="港股红利低波数据初始化")
    parser.add_argument("--industry", default="", help="行业名称(东财港股行业), 空=全市场")
    parser.add_argument("--start", type=int, default=2024, help="起始年份")
    parser.add_argument("--end", type=int, default=2025, help="结束年份")
    parser.add_argument("--years", type=int, nargs="*", help="指定年份列表 (覆盖 --start/--end)")
    parser.add_argument("--max-stocks", type=int, default=3000, help="最多扫描股票数(默认3000覆盖全市场2782只)")
    parser.add_argument("--codes", default="", help="指定港股代码(逗号分隔), 如 00700,00005; 优先级最高")
    parser.add_argument("--sleep", type=float, default=0.1, help="每次接口调用的间隔秒数")
    parser.add_argument("--build-industry", action="store_true", help="仅重建港股行业映射缓存后退出")
    args = parser.parse_args()

    # 确保表结构存在
    await pg_service.init_hk_rlv_schema()

    if args.build_industry:
        print("重建港股行业映射缓存 ...")
        mp = await hkd.industry_map(use_cache=False)
        print(f"行业映射: {len(mp)} 只")
        return

    # 行业映射 (供行业过滤/入库)
    ind_map = await hkd.industry_map()
    print(f"行业映射: {len(ind_map)} 只")

    years = list(range(args.start, args.end + 1))
    if args.years:
        years = args.years

    codes: list[str] | None = None
    if args.codes:
        codes = [hkd._symbol_to_ts_code(c) for c in args.codes.split(",") if c.strip()]

    if codes:
        # 指定股票模式
        rows = []
        failed = 0
        for ts_code in codes:
            try:
                m = await hkd.stock_metrics(ts_code, ind_map)
                for y in years:
                    r = await hkrlv.compute_stock_row(m, y)
                    if r is not None:
                        rows.append(r)
            except Exception as e:
                print(f"  [{ts_code}] 失败: {e}")
                failed += 1
            if args.sleep > 0:
                await asyncio.sleep(args.sleep)
        stored = await pg_service.upsert_hk_rlv_rows(rows)
        print(f"\n完成: 指定 {len(codes)} 只, 入库 {stored} 行, 失败 {failed}")
        return

    print(f"初始化: 行业={args.industry or '全市场'} 年份={years} "
          f"max_stocks={args.max_stocks} sleep={args.sleep}")

    t0 = time.time()
    result = await hkrlv.sync_industry_years(args.industry, years,
                                             max_stocks=args.max_stocks, sleep=args.sleep)
    print(f"\n完成: 扫描 {result['scanned_total']} 条记录, 入库 {result['stored_total']} 条, "
          f"耗时 {time.time() - t0:.0f} 秒")
    for y, v in result["per_year"].items():
        print(f"  {y}: scanned={v['scanned']} stored={v['stored']} failed={v['failed']}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
