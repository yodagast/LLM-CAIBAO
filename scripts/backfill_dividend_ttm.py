"""回填 red_low_vol 表的 股息率-TTM (dividend_yield_ttm) 与 上个交易日收盘价 (last_close)。

背景:
  股息率拆分为 静态股息率 (当年分红/年末收盘价, 即 dividend_yield) 与
  股息率-TTM (当年分红/上个交易日收盘价, 即 dividend_yield_ttm), 并展示上个交易日
  收盘价 (last_close)。

实现:
  用 tushare daily(trade_date=最新交易日) 一次批量取全市场收盘价 (约1次调用),
  再回写表内所有行; 停牌股 (不在该日结果中) 用 _latest_close 单只回退。

用法:
  python scripts/backfill_dividend_ttm.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from api import data_service as ds  # noqa: E402
from api import pg_service as pg  # noqa: E402
from api import redlowvol_service as rlv  # noqa: E402


async def main() -> None:
    await pg.init_schema()  # 确保 dividend_yield_ttm / last_close 列存在
    pro = ds._init_pro()

    # 1. 取最新交易日 (探针一只股票的最新日线)
    probe = await pro.daily(ts_code="000001.SZ", fields="trade_date,close")
    if probe is None or probe.empty:
        print("无法获取最新交易日")
        return
    latest_date = str(probe.sort_values("trade_date").iloc[-1]["trade_date"])
    print("最新交易日:", latest_date)

    # 2. 批量取该日全市场收盘价 (一次调用)
    full = await pro.daily(trade_date=latest_date, fields="ts_code,close")
    close_map: dict[str, float] = {}
    if full is not None and not full.empty:
        for _, r in full.iterrows():
            try:
                close_map[str(r["ts_code"])] = float(r["close"])
            except Exception:
                pass
    print(f"该日全市场 {len(close_map)} 只")

    # 3. 读表内所有行
    pool = await pg._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT ts_code, year, div_per_share FROM red_low_vol")

    by_stock: dict[str, list] = {}
    for r in rows:
        by_stock.setdefault(r["ts_code"], []).append(r)

    # 4. 停牌股 (不在该日结果中) 单只回退
    missing = [c for c in by_stock if c not in close_map]
    print(f"停牌/缺收盘价需回退 {len(missing)} 只")
    for i, c in enumerate(missing):
        v = await rlv._latest_close(pro, c)
        if v and v > 0:
            close_map[c] = v
        if i % 500 == 0:
            await asyncio.sleep(0.1)

    # 5. 计算股息率-TTM 并回写
    updated = 0
    async with pool.acquire() as conn:
        for ts_code, srows in by_stock.items():
            close = close_map.get(ts_code)
            if not close or close <= 0:
                continue
            for r in srows:
                div = r["div_per_share"]
                ttm = (div / close * 100.0) if div and div > 0 else None
                await conn.execute(
                    "UPDATE red_low_vol SET last_close=$1, dividend_yield_ttm=$2, updated_at=now() "
                    "WHERE ts_code=$3 AND year=$4",
                    close, ttm, ts_code, r["year"],
                )
                updated += 1
    print(f"完成: 更新 {updated} 行")


if __name__ == "__main__":
    asyncio.run(main())
