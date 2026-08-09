"""港股红利低波 + 基本面 全市场数据初始化脚本 (一次遍历同时入库两个表)。

用法:
  python scripts/init_hk_all_market.py --start 2023 --end 2025      # 全市场 2023~2025
  python scripts/init_hk_all_market.py --years 2025                 # 仅 2025
  python scripts/init_hk_all_market.py --industry 银行              # 仅银行行业
  python scripts/init_hk_all_market.py --codes 00700,00005 --years 2025  # 指定股票调试
  python scripts/init_hk_all_market.py --workers 4                  # 4 线程并行加速
  python scripts/init_hk_all_market.py --force                      # 忽略断点, 全量重算

说明:
  - 数据源: 东财港股财务/分红/资产负债表 + 腾讯港股日线 + tushare hk_basic (股票列表)
  - 每只股票一次拉取基础数据 (财务指标/分红/资产负债表/K线), 同时计算并入库
    hk_red_low_vol (红利低波) 与 hk_fundamental_screen (基本面) 两张表, 比分开跑省一半接口调用
  - 自动剔除 RMB 柜台股 (-R, 如 中国移动-R 80941.HK)
  - 断点续跑: 已同步 (两表给定年份全部有数据) 的股票自动跳过; --force 全量重算
  - 批量提交 (默认每 50 只提交一次), 中断不丢进度, 可重复续跑
  - 全市场约 2782 只 × ~4 次接口调用, 单线程预计 1.5~3 小时, 建议 --workers 4~8 加速
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import hk_data_service as hkd  # noqa: E402
from api import hk_fundamental_service as hkf  # noqa: E402
from api import hk_redlowvol_service as hkrlv  # noqa: E402
from api import pg_service  # noqa: E402


def _process_stock(ts_code: str, ind_map: dict, years: list[int]) -> tuple[list, list] | None:
    """处理单只股票: 一次拉取基础数据, 返回 (rlv_rows, fund_rows); 失败返回 None。

    异常需向调用方区分「真实无数据」与「接口异常」。真实无数据 (两表均无行) 返回 ([], []),
    调用方视为「已尝试、无数据」不计失败; 接口异常返回 None 计失败。
    """
    try:
        m = hkd.stock_metrics(ts_code, ind_map)
    except Exception:
        return None
    rlv: list = []
    fund: list = []
    for y in years:
        r = hkrlv.compute_stock_row(m, y)
        if r is not None:
            rlv.append(r)
        f = hkf.compute_stock_row(m, y)
        if f is not None:
            fund.append(f)
    return rlv, fund


def _flush(rlv_rows: list, fund_rows: list) -> None:
    """批量 upsert 两个表并清空待提交列表。"""
    if rlv_rows:
        pg_service.upsert_hk_rlv_rows(rlv_rows)
        rlv_rows.clear()
    if fund_rows:
        pg_service.upsert_hk_fundamental_rows(fund_rows)
        fund_rows.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="港股红利低波+基本面 全市场初始化")
    parser.add_argument("--start", type=int, default=2024, help="起始年份")
    parser.add_argument("--end", type=int, default=2025, help="结束年份")
    parser.add_argument("--years", type=int, nargs="*", help="指定年份列表 (覆盖 --start/--end)")
    parser.add_argument("--industry", default="", help="行业名称(东财港股行业), 空=全市场")
    parser.add_argument("--codes", default="", help="指定港股代码(逗号分隔), 如 00700,00005; 优先级最高")
    parser.add_argument("--limit", type=int, default=0, help="最多处理股票数 (0=不限, 调试用)")
    parser.add_argument("--workers", type=int, default=1, help="并行线程数 (1=串行, 建议 4~8)")
    parser.add_argument("--batch", type=int, default=50, help="每 N 只股票提交一次入库")
    parser.add_argument("--sleep", type=float, default=0.0, help="串行模式下每次调用间隔秒数")
    parser.add_argument("--force", action="store_true", help="忽略断点续跑, 全量重算")
    parser.add_argument("--progress", type=int, default=50, help="每 N 只打印一次进度")
    args = parser.parse_args()

    # 确保两张表存在
    pg_service.init_hk_rlv_schema()
    pg_service.init_hk_fundamental_schema()

    years = list(range(args.start, args.end + 1))
    if args.years:
        years = args.years
    if not years:
        print("年份列表为空。"); return

    print(f"行业映射加载中 ...")
    ind_map = hkd.industry_map()
    print(f"行业映射: {len(ind_map)} 只")

    # 候选股票列表 (剔除 -R 柜台股)
    stocks = hkd.hk_stock_list()
    codes: list[str] = []
    if args.codes:
        codes = [hkd._symbol_to_ts_code(c) for c in args.codes.split(",") if c.strip()]
    else:
        for _, row in stocks.iterrows():
            ts_code = str(row["ts_code"])
            name = str(row.get("name") or "")
            if name.rstrip().endswith("-R"):
                continue
            if args.industry and args.industry not in ind_map.get(ts_code, ""):
                continue
            codes.append(ts_code)
    if args.limit and args.limit > 0:
        codes = codes[:args.limit]

    # 断点续跑: 已同步股票跳过
    if args.codes or args.force:
        done_set: set = set()
    else:
        done_set = pg_service.hk_synced_ts_codes(years)
    pending = [c for c in codes if c not in done_set]
    print(f"候选 {len(codes)} 只 (行业={args.industry or '全市场'}, 年份={years}) "
          f"| 已同步跳过 {len(done_set)} | 待处理 {len(pending)}"
          + (" | 并行 workers=" + str(args.workers) if args.workers > 1 else ""))
    if not pending:
        print("全部已同步, 无需处理。")
        return

    rlv_rows: list = []
    fund_rows: list = []
    ok = failed = no_data = 0
    t0 = time.time()

    def _flush_if_batch(i: int) -> None:
        """每 batch 只提交一次 (串行按股票数, 并行按完成数)。"""
        if i % args.batch == 0:
            _flush(rlv_rows, fund_rows)

    if args.workers > 1:
        # 并行模式: 各线程拉数据, 主线程收集并按完成数批量提交
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_process_stock, c, ind_map, years): c for c in pending}
            done_count = 0
            for fut in as_completed(futures):
                r = fut.result()
                if r is None:
                    failed += 1
                elif not r[0] and not r[1]:
                    no_data += 1
                else:
                    ok += 1
                    rlv_rows.extend(r[0])
                    fund_rows.extend(r[1])
                done_count += 1
                _flush_if_batch(done_count)
                if done_count % args.progress == 0:
                    _report(done_count, len(pending), ok, failed, no_data, t0)
        _flush(rlv_rows, fund_rows)
    else:
        # 串行模式: 逐只处理 + 批量提交
        for i, ts_code in enumerate(pending, 1):
            r = _process_stock(ts_code, ind_map, years)
            if r is None:
                failed += 1
            elif not r[0] and not r[1]:
                no_data += 1
            else:
                ok += 1
                rlv_rows.extend(r[0])
                fund_rows.extend(r[1])
            _flush_if_batch(i)
            if i % args.progress == 0:
                _report(i, len(pending), ok, failed, no_data, t0)
            if args.sleep > 0:
                time.sleep(args.sleep)
        _flush(rlv_rows, fund_rows)

    _report(len(pending), len(pending), ok, failed, no_data, t0, final=True)


def _report(done: int, total: int, ok: int, failed: int, no_data: int,
            t0: float, final: bool = False) -> None:
    elapsed = time.time() - t0
    if done > 0:
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate / 60 if rate > 0 else 0
    else:
        rate = 0.0
        eta = 0.0
    tag = "完成" if final else f"进度 {done}/{total}"
    print(f"[{tag}] 耗时 {elapsed:.0f}s | 成功 {ok} | 无数据 {no_data} | 失败 {failed}"
          + (f" | 速率 {rate:.1f}只/s | 预计剩余 {eta:.0f}分钟" if rate > 0 else ""))


if __name__ == "__main__":
    main()
