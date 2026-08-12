"""全量重算 red_low_vol 表的分红字段 (每股分红/静态股息率/股息率TTM/分红率/3年股利增长/上日收盘)。

背景:
  之前的分红修复 (fix_dividend_data.py + backfill_dividend_ttm.py) 发现部分行
  (如 600585.SH 2025 每股分红 0.61, 正确应为 中期0.24+年度0.61=0.85) 未被修正,
  数据库存在陈旧/错误分红数据。本脚本按用户要求"删除旧有数据库, 从新开始计算所有
  公司正确的动态股息率": 全部 6 个分红字段一律按最新 tushare dividend 重算
  (一年多次分红按 end_date 年份求和 + (end_date,cash_div) 去重), 股息率-TTM 用
  批量最新交易日收盘价; 静态股息率因年末收盘价未入库, 用旧股息率 × (新/旧每股分红)
  等比修正 (旧股息率由同一收盘价计算, 等比缩放精确)。

效率: 每只股票仅 1 次 pro.dividend 调用 + 1 次批量 daily(trade_date=最新日) 收盘价。

用法:
  python scripts/recompute_dividend_all.py                 # 全量
  python scripts/recompute_dividend_all.py --codes 600585.SH   # 只处理指定股票
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from api import data_service as ds  # noqa: E402
from api import pg_service as pg  # noqa: E402
from api import redlowvol_service as rlv  # noqa: E402


async def _div_map(pro, ts_code: str) -> dict[int, float] | None:
    """每只股票全年已实施每股现金红利之和 {year: total} (按 end_date 年份求和 + 去重)。

    返回 None 表示 tushare 接口错误/限频 (应跳过, 不能当作无分红置空);
    返回 {} 表示该股确实无分红记录。
    """
    try:
        dv = await pro.dividend(ts_code=ts_code)
    except Exception:
        return None
    if dv is None or dv.empty or "cash_div" not in dv.columns:
        return {}
    dv = dv.copy()
    dv["_cash"] = pd.to_numeric(dv["cash_div"], errors="coerce")
    impl = dv[dv["div_proc"] == "实施"].drop_duplicates(subset=["end_date", "cash_div"])
    out: dict[int, float] = {}
    for _, r in impl.iterrows():
        try:
            y = int(str(r["end_date"])[:4])
        except (TypeError, ValueError):
            continue
        c = ds._to_float(r.get("cash_div"))
        if c is not None and c > 0:
            out[y] = out.get(y, 0.0) + c
    return out


async def _div_map_with_retry(pro, ts_code: str) -> dict[int, float] | None:
    """获取分红映射, 接口错误/限频时退避重试 (最多 3 次)。"""
    for attempt in range(3):
        m = await _div_map(pro, ts_code)
        if m is not None:
            return m
        await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _bulk_latest_close(pro) -> dict[str, float]:
    """批量取最新交易日 (上个交易日) 全市场收盘价 {ts_code: close}; 停牌股单只回退。"""
    probe = await pro.daily(ts_code="000001.SZ", fields="trade_date,close")
    if probe is None or probe.empty:
        return {}
    latest_date = str(probe.sort_values("trade_date").iloc[-1]["trade_date"])
    print("最新交易日:", latest_date)
    full = await pro.daily(trade_date=latest_date, fields="ts_code,close")
    m: dict[str, float] = {}
    if full is not None and not full.empty:
        for _, r in full.iterrows():
            try:
                m[str(r["ts_code"])] = float(r["close"])
            except Exception:
                pass
    return m


async def main() -> None:
    parser = argparse.ArgumentParser(description="全量重算 red_low_vol 分红字段")
    parser.add_argument("--codes", default="", help="只处理指定 ts_code, 逗号分隔")
    parser.add_argument("--sleep", type=float, default=0.1, help="dividend 调用间隔秒数")
    args = parser.parse_args()

    await pg.init_schema()
    pro = ds._init_pro()

    pool = await pg._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts_code, year, eps, div_per_share AS old_div, "
            "dividend_yield AS old_yield FROM red_low_vol"
        )
    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        rows = [r for r in rows if r["ts_code"] in want]

    by_stock: dict[str, list] = {}
    for r in rows:
        by_stock.setdefault(r["ts_code"], []).append(r)
    print(f"共 {len(rows)} 行 / {len(by_stock)} 只股票")

    close_map = await _bulk_latest_close(pro)
    missing = [c for c in by_stock if c not in close_map]
    print(f"停牌/缺收盘价需回退 {len(missing)} 只")
    for i, c in enumerate(missing):
        v = await rlv._latest_close(pro, c)
        if v and v > 0:
            close_map[c] = v
        if i % 500 == 0:
            await asyncio.sleep(0.1)

    updates: list[tuple] = []
    n = 0
    total = 0
    t0 = time.time()

    async def flush(conn) -> None:
        nonlocal total
        if not updates:
            return
        for (ts_code, y, div, ny, nttm, np_, ng, close) in updates:
            await conn.execute(
                "UPDATE red_low_vol SET div_per_share=$1, dividend_yield=$2, "
                "dividend_yield_ttm=$3, payout_ratio=$4, dividend_growth_3y=$5, "
                "last_close=$6, updated_at=now() "
                "WHERE ts_code=$7 AND year=$8",
                div, ny, nttm, np_, ng, close, ts_code, y,
            )
        total += len(updates)
        updates.clear()

    async with pool.acquire() as conn:
        for ts_code, srows in by_stock.items():
            dm = await _div_map_with_retry(pro, ts_code)
            await asyncio.sleep(args.sleep)
            close = close_map.get(ts_code)
            n += 1
            if dm is None:
                # dividend 接口失败 (限频/网络), 跳过该股保留原值
                print(f"  !! {ts_code} dividend 获取失败, 跳过 (保留原值)", flush=True)
                continue
            for r in srows:
                y = r["year"]
                nd = dm.get(y)
                if nd is None or nd <= 0:
                    # 该年无已实施分红: 分红字段置空 (保留 last_close)
                    updates.append((ts_code, y, None, None, None, None, None, close))
                    continue
                old_div = r["old_div"]
                old_yield = r["old_yield"]
                # 静态股息率 = 全年分红/年末收盘价 (表未存年末收盘价, 用旧值等比修正)
                ny = None
                if old_div and old_yield is not None and old_div > 0:
                    ny = old_yield * (nd / old_div)
                # 股息率-TTM = 全年分红/上个交易日收盘价
                nttm = (nd / close * 100.0) if close and close > 0 else None
                # 分红率 = 全年分红 / EPS
                np_ = None
                eps = r["eps"]
                if eps and eps > 0:
                    np_ = nd / eps * 100.0
                # 3年股利复合增长
                d0, d3 = dm.get(y), dm.get(y - 3)
                ng = None
                if d0 and d3 and d0 > 0 and d3 > 0:
                    ng = (pow(d0 / d3, 1.0 / 3.0) - 1.0) * 100.0
                updates.append((ts_code, y, nd, ny, nttm, np_, ng, close))
            if n % 500 == 0:
                print(f"  已处理 {n}/{len(by_stock)} 只, 累计更新 {total} 条, "
                      f"耗时 {time.time() - t0:.0f}s", flush=True)
                await flush(conn)
        print(f"  扫描完成, 待写入 {len(updates)} 条, 耗时 {time.time() - t0:.0f}s", flush=True)
        await flush(conn)
    print(f"完成: 共更新 {total} 行")


if __name__ == "__main__":
    asyncio.run(main())
