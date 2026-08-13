"""回填 A股选股新字段: 毛利率 gross_margin / 自由现金流 free_cashflow。

背景:
  选股 tab 新增「自由现金流≥ / 毛利率≥」筛选后, red_low_vol / fundamental_screen
  新加了 gross_margin(毛利率%) 与 free_cashflow(自由现金流, 万元) 列, 但旧库行
  这些列均为 NULL。本脚本对 A股全市场按 ts_code 一次拉取 fina_indicator (仅年报),
  重算:
    gross_margin   = grossprofit_margin (%)      (tushare 的 gross_margin 是毛利额元, 勿用)
    free_cashflow  = fcff / 10000 (万元)         (tushare fcff 单位为元)
  回写 red_low_vol 与 fundamental_screen 两表 (幂等 UPDATE, 可重复续跑)。

效率: 每只股票 1 次 pro.fina_indicator 调用 (拿全部年报), 全市场约 5536 只。
默认只更新「任一新字段为 NULL」的行 (断点续跑); --force 全量覆盖重算。

用法:
  python scripts/backfill_margin_fcf.py                    # 全市场回填新字段
  python scripts/backfill_margin_fcf.py --industry 白酒    # 仅某行业
  python scripts/backfill_margin_fcf.py --codes 600519.SH,000858.SZ
  python scripts/backfill_margin_fcf.py --limit 100        # 调试: 只处理前 100 只
  nohup python scripts/backfill_margin_fcf.py > logs/backfill.log 2>&1 &   # 后台运行
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import data_service as ds  # noqa: E402
from api import pg_service as pg  # noqa: E402


async def _fina_year_map(pro, ts_code: str) -> dict[int, dict] | None:
    """一次拉取该股全部 fina_indicator 年报, 返回 {year: {gross_margin, free_cashflow}}。

    仅取 end_date 以 1231 结尾的年报 (避免一季/中报/三季干扰毛利率); 同一年取最新一期。
    返回 None 表示 tushare 接口错误/限频 (调用方应跳过, 不能当作无数据);
    返回 {} 表示该股无年报数据。
    """
    try:
        df = await pro.fina_indicator(ts_code=ts_code)
    except Exception:
        return None
    if df is None or df.empty:
        return {}
    df = df[df["end_date"].astype(str).str.endswith("1231")].copy()
    out: dict[int, dict] = {}
    for _, r in df.iterrows():
        try:
            y = int(str(r.get("end_date"))[:4])
        except (TypeError, ValueError):
            continue
        gm = ds._to_float(r.get("grossprofit_margin"))
        fcff = ds._to_float(r.get("fcff"))
        fcf = (fcff / 10000.0) if fcff is not None else None
        # 同一年多期(少见)取后写, 即最新一期
        out[y] = {"gross_margin": gm, "free_cashflow": fcf}
    return out


async def _fina_year_map_retry(pro, ts_code: str) -> dict[int, dict] | None:
    """接口错误/限频时退避重试 (最多 3 次)。"""
    for attempt in range(3):
        m = await _fina_year_map(pro, ts_code)
        if m is not None:
            return m
        await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _collect_tasks(industry: str, codes: list[str], limit: int, force: bool) -> list[tuple[str, int]]:
    """收集待回填 (ts_code, year): 两表并集; 默认仅取任一新字段为 NULL 的行。

    返回按 ts_code 聚合前的扁平列表 [(ts_code, year), ...]。
    """
    pool = await pg._get_pool()
    tasks: set[tuple[str, int]] = set()
    async with pool.acquire() as conn:
        for table, cond in (
            ("red_low_vol", "gross_margin IS NULL OR free_cashflow IS NULL"),
            ("fundamental_screen", "gross_margin IS NULL OR free_cashflow IS NULL"),
        ):
            sql = f"SELECT ts_code, year FROM {table}"
            w = []
            if industry:
                w.append("industry LIKE $1")
            if not force:
                w.append(cond)
            if w:
                sql += " WHERE " + " AND ".join(w)
            params: list = []
            if industry:
                params.append(f"%{industry}%")
            rows = await conn.fetch(sql, *params)
            for r in rows:
                tasks.add((str(r["ts_code"]), int(r["year"])))
    if codes:
        want = set(codes)
        tasks = {(c, y) for (c, y) in tasks if c in want}
    if limit and limit > 0:
        # 按 ts_code 顺序截取 (保持稳定, 便于断点续跑)
        by_stock = sorted({c for c, _ in tasks})
        want_codes = set(by_stock[:limit])
        tasks = {(c, y) for (c, y) in tasks if c in want_codes}
    return sorted(tasks)


async def main() -> None:
    parser = argparse.ArgumentParser(description="回填 A股选股新字段 (毛利率/自由现金流)")
    parser.add_argument("--industry", default="", help="行业名称(东财分类), 空=全市场")
    parser.add_argument("--codes", default="", help="只处理指定 ts_code, 逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="最多处理股票数(0=不限; 调试用)")
    parser.add_argument("--sleep", type=float, default=0.05, help="fina_indicator 调用间隔秒数")
    parser.add_argument("--force", action="store_true", help="全量覆盖重算 (默认只回填 NULL 行)")
    args = parser.parse_args()

    await pg.init_schema()
    await pg.init_fundamental_schema()
    pro = ds._init_pro()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    tasks = await _collect_tasks(args.industry, codes, args.limit, args.force)
    by_stock: dict[str, list[int]] = {}
    for c, y in tasks:
        by_stock.setdefault(c, []).append(y)
    print(f"待回填: {len(by_stock)} 只股票 / {len(tasks)} 行"
          f"{' · 行业=' + args.industry if args.industry else ''}"
          f"{' · 强制全量' if args.force else ' · 仅 NULL 行'}")

    pool = await pg._get_pool()
    t0 = time.time()
    n_done = 0
    n_rows = 0
    n_missing = 0
    n_err = 0

    async def flush(conn, rows) -> None:
        nonlocal n_rows
        if not rows:
            return
        if rows[0][4] == "rlv":  # (ts_code, year, gm, fcf, table)
            await conn.executemany(
                "UPDATE red_low_vol SET gross_margin=$1, free_cashflow=$2 "
                "WHERE ts_code=$3 AND year=$4", [r[:4] for r in rows])
        else:
            await conn.executemany(
                "UPDATE fundamental_screen SET gross_margin=$1, free_cashflow=$2 "
                "WHERE ts_code=$3 AND year=$4", [r[:4] for r in rows])
        n_rows += len(rows)

    rlv_updates: list[tuple] = []
    fund_updates: list[tuple] = []
    # 按 ts_code 顺序处理 (tasks 已排序)
    for i, (code, years) in enumerate(by_stock.items()):
        m = await _fina_year_map_retry(pro, code)
        if m is None:
            n_err += 1
        elif not m:
            n_missing += 1
        else:
            for y in years:
                d = m.get(y)
                if d is None:
                    continue
                gm, fcf = d["gross_margin"], d["free_cashflow"]
                # 默认模式: 仅当待回填且新字段本就为 NULL 时才写; 覆盖模式总是写
                if gm is None and fcf is None:
                    continue
                rlv_updates.append((gm, fcf, code, y, "rlv"))
                fund_updates.append((gm, fcf, code, y, "fund"))
        n_done += 1
        if i % 200 == 0 and (rlv_updates or fund_updates):
            async with pool.acquire() as conn:
                await flush(conn, rlv_updates)
                await flush(conn, fund_updates)
            rlv_updates.clear()
            fund_updates.clear()
            el = time.time() - t0
            print(f"  进度 {n_done}/{len(by_stock)} · 已写 {n_rows} 行 · 耗时 {el:.0f}s", flush=True)
        if args.sleep > 0:
            await asyncio.sleep(args.sleep)

    # 剩余批量提交
    async with pool.acquire() as conn:
        await flush(conn, rlv_updates)
        await flush(conn, fund_updates)

    dt = (time.time() - t0) / 60
    print(f"\n完成: 处理 {n_done} 只 (无年报数据 {n_missing}, 接口失败 {n_err}), "
          f"回写 {n_rows} 行, 耗时 {dt:.1f} 分钟")


if __name__ == "__main__":
    asyncio.run(main())
