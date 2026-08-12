"""修复 red_low_vol 表的分红字段 (每股分红 / 股息率 / 分红率 / 3年股利增长)。

背景:
  原实现把 tushare dividend 按 end_date 精确匹配 YYYY1231 且只取单条'实施'记录,
  忽略了 中期(0630)/三季(0930) 等多期分红, 一年多次分红的公司 (中国移动/中国电信/
  五粮液/招商银行/贵州茅台等) 每股分红与股息率被低估约 50%。另外 tushare 偶发
  同一 end_date 出现重复'实施'记录 (如茅台 20251231 两条 28.02423), 求和前需按
  (end_date, cash_div) 去重。

本脚本针对表内已有 (ts_code, year) 行, 每只股票仅 1 次 pro.dividend 调用, 重算
4 个分红字段并 UPDATE。股息率因表未存年末收盘价, 用旧股息率 × (新每股分红 / 旧
每股分红) 等比修正 (旧股息率由同一收盘价计算, 故等比缩放精确)。

用法:
  python scripts/fix_dividend_data.py                    # 全量修复
  python scripts/fix_dividend_data.py --limit-stocks 10  # 仅测试前 10 只
  python scripts/fix_dividend_data.py --codes 000858.SZ,600036.SH
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


async def _div_map(pro, ts_code: str) -> dict[int, float]:
    """每只股票全年已实施每股现金红利之和 {year: total} (去重+求和, 与代码一致)。"""
    try:
        dv = await pro.dividend(ts_code=ts_code)
    except Exception:
        return {}
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


async def main() -> None:
    parser = argparse.ArgumentParser(description="修复 red_low_vol 分红字段")
    parser.add_argument("--limit-stocks", type=int, default=0, help="只处理前 N 只股票 (0=全部)")
    parser.add_argument("--codes", default="", help="只处理指定 ts_code, 逗号分隔 (覆盖 --limit-stocks)")
    parser.add_argument("--sleep", type=float, default=0.1, help="tushare 调用间隔秒数")
    args = parser.parse_args()

    await pg.init_schema()
    pro = ds._init_pro()

    pool = await pg._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts_code, year, eps, div_per_share AS old_div, "
            "dividend_yield AS old_yield FROM red_low_vol "
            "ORDER BY ts_code, year"
        )

    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        rows = [r for r in rows if r["ts_code"] in want]
    elif args.limit_stocks > 0:
        keep = set()
        for r in rows:
            keep.add(r["ts_code"])
            if len(keep) >= args.limit_stocks:
                break
        rows = [r for r in rows if r["ts_code"] in keep]

    by_stock: dict[str, list] = {}
    for r in rows:
        by_stock.setdefault(r["ts_code"], []).append(r)
    print(f"共 {len(rows)} 行 / {len(by_stock)} 只股票待处理")

    updates: list[tuple] = []
    total_updated = 0
    n = 0
    t0 = time.time()

    async def flush(conn) -> None:
        """批量提交当前积累的更新, 避免最后一次性大事务 (中途失败不丢已处理结果)。"""
        nonlocal total_updated
        if not updates:
            return
        for (ts_code, y, nd, ny, np_, ng) in updates:
            await conn.execute(
                "UPDATE red_low_vol SET div_per_share=$1, dividend_yield=$2, "
                "payout_ratio=$3, dividend_growth_3y=$4, updated_at=now() "
                "WHERE ts_code=$5 AND year=$6",
                nd, ny, np_, ng, ts_code, y,
            )
        total_updated += len(updates)
        updates.clear()

    async with pool.acquire() as conn:
        for ts_code, srows in by_stock.items():
            dm = await _div_map(pro, ts_code)
            await asyncio.sleep(args.sleep)
            n += 1
            for r in srows:
                y = r["year"]
                nd = dm.get(y)
                if nd is None or nd <= 0:
                    continue  # 该年无分红数据, 保持原值
                old_div = r["old_div"]
                old_yield = r["old_yield"]
                # 股息率: 表未存年末收盘价, 用旧股息率等比修正
                ny = None
                if old_div and old_yield is not None and old_div > 0:
                    ny = old_yield * (nd / old_div)
                # 分红率 = 每股分红 / EPS
                np_ = None
                eps = r["eps"]
                if eps and eps > 0:
                    np_ = nd / eps * 100.0
                # 3年股利复合增长
                d0, d3 = dm.get(y), dm.get(y - 3)
                ng = None
                if d0 and d3 and d0 > 0 and d3 > 0:
                    ng = (pow(d0 / d3, 1.0 / 3.0) - 1.0) * 100.0
                updates.append((ts_code, y, nd, ny, np_, ng))
            if n % 500 == 0:
                print(f"  已处理 {n}/{len(by_stock)} 只, 待写入 {len(updates)} 条, "
                      f"累计更新 {total_updated} 条, 耗时 {time.time() - t0:.0f}s", flush=True)
                await flush(conn)
        print(f"  扫描完成, 待写入 {len(updates)} 条, 耗时 {time.time() - t0:.0f}s", flush=True)
        await flush(conn)
    print(f"完成: 共更新 {total_updated} 行")


if __name__ == "__main__":
    asyncio.run(main())
