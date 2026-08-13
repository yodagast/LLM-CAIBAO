"""从 PostgreSQL 导出股票池日线为 qlib 数据目录 (Alpha158 可直接使用)。

复用 api/alpha158_service.build_qlib_dataset: 多股共享交易日历, 前复权, 缺日 NaN。
参考 ashare-lowfreq-research 的 import-sqlite(-> Parquet) + 构建 qlib provider 流程,
本项目落地为「PostgreSQL -> qlib data dir」。

qlib .bin 格式: 小端 float32 数组, 首元素=起始日历索引, 之后按日历逐日对齐。
价格默认前复权(qfq): price_adj = price * adj_factor / latest_adj_factor
(统一缩放保持 OHLC 比率, 使收益率/Alpha158 标签正确; volume 保持原始)。

用法:
  .venv/bin/python scripts/export_qlib_data.py                               # 默认招商银行
  .venv/bin/python scripts/export_qlib_data.py --codes 600036.SH,000858.SZ
  .venv/bin/python scripts/export_qlib_data.py --codes 600036.SH --start 20160101 --end 20260812
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import alpha158_service  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL -> qlib 数据目录导出(多股)")
    parser.add_argument("--codes", default="600036.SH", help="股票代码(逗号分隔), 默认招商银行")
    parser.add_argument("--qlib-dir", default=str(alpha158_service.DEFAULT_QLIB_DIR),
                        help="qlib provider_uri")
    parser.add_argument("--adj", choices=["qfq", "raw"], default="qfq", help="复权方式")
    parser.add_argument("--start", default="20160101", help="起始日 YYYYMMDD")
    parser.add_argument("--end", default="", help="结束日 YYYYMMDD, 空=今天")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    result = await alpha158_service.build_qlib_dataset(
        codes, Path(args.qlib_dir), start=args.start, end=args.end, adj=args.adj)

    print(f"qlib 数据目录: {result['qlib_dir']}")
    print(f"交易日历: {result['calendar_days']} 天")
    for sym, info in result["instruments"].items():
        print(f"  {sym}: {info['start']} ~ {info['end']}  {info['rows']} 行  "
              f"latest_adj={info['latest_adj']:.4f}")
    for s in result["skipped"]:
        print(f"  跳过 {s['ts_code']}: {s['reason']}")
    if not result["instruments"]:
        raise SystemExit("无可用股票数据, 请先运行 scripts/sync_tushare_pg.py 同步")


if __name__ == "__main__":
    asyncio.run(main())
