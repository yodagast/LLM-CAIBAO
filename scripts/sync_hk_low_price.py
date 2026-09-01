"""港股低价选股全市场同步脚本: 计算全市场接近 52 周低点的港股并写入 pgsql。

用法:
  uv run python scripts/sync_hk_low_price.py [--max-dev 15]

流程:
  - 全市场扫描 tushare hk_basic 全部港股 (剔除 -R 柜台股, 约 2700 只)
  - 每只取腾讯日线聚合月线算 52 周高低点 + 最近收盘/涨跌幅 + 东财财务指标
  - 筛选 close >= week52_low 且偏离度 <= 阈值, 按偏离度升序 (全部命中入库)
  - 结果按 (calc_date, ts_code) 幂等 upsert 入库 hk_low_price_screen 表
  - calc_date = 腾讯 K 线最新交易日 (00700.HK 锚点)
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import hk_low_price_service as hklps  # noqa: E402
from scripts.download_annual_reports import _load_env  # noqa: E402


async def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="港股低价选股全市场同步 (接近52周低点 → pgsql)")
    parser.add_argument("--max-dev", type=float, default=15.0,
                        help="最大偏离阈值 %%: 最近收盘价相对52周最低价的最大涨幅 (默认15)")
    args = parser.parse_args()

    t0 = time.time()
    print(f"港股低价选股同步: 全市场, 最大偏离 52周低点 ≤ {args.max_dev}% …")
    stored = await hklps.hk_sync_low_price_to_db(max_dev_pct=args.max_dev)
    dt = (time.time() - t0) / 60
    print(f"\n完成: 入库 {stored} 行 (全部命中), 耗时 {dt:.1f} 分钟")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())