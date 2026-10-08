#!/usr/bin/env python3
"""排查「前端行情/日线数据冻结在旧日期」问题 (线上部署首选工具)。

背景: 前端详情页 / 自选股「最近收盘」优先读本地 PostgreSQL (stock_daily_bars)。
若线上数据看起来停在某个历史日期, 原因通常是以下之一 —— 本脚本逐层检查并给出
结论, 加 --fix 可直接补数修复:

  1. 环境/连接   : .env / DATABASE_URL / 库表是否存在
  2. 数据覆盖    : stock_daily_bars 各 kind(stock/fund/hk) 的最新交易日 vs 应有交易日
  3. 目标缺失    : 「我的股票 ∪ 策略Hub」里哪些标的最新日落后 (前端会读不到/显示旧值)
  4. 定时任务    : crontab 是否有 nightly / sync_target_daily 条目, 锁文件是否残留
  5. 日志        : 最近一次 nightly 日志的失败步骤
  6. --fix       : 对落后的标的重新跑增量同步 (港股走腾讯, A股/ETF 走 tushare)

用法:
    python scripts/check_daily_data.py                 # 仅体检 (只读, 安全)
    python scripts/check_daily_data.py --fix           # 顺带补齐落后的日线
    python scripts/check_daily_data.py --days 7        # 落后超过 7 个自然日才算异常
    python scripts/check_daily_data.py --kind hk       # 只检查港股
"""

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def hr(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def mask_dsn(dsn: str) -> str:
    """隐藏 DATABASE_URL 里的密码。"""
    if "@" not in dsn:
        return dsn
    head, _, tail = dsn.partition("@")
    if ":" in head:
        head = head.rsplit(":", 1)[0] + ":***"
    return f"{head}@{tail}"


async def check_env() -> bool:
    hr("1) 环境与数据库连接")
    print(f"项目根目录 : {PROJECT_ROOT}")
    print(f"Python     : {sys.executable}")
    print(f"当前时间   : {datetime.now():%Y-%m-%d %H:%M:%S}")

    env_file = PROJECT_ROOT / ".env"
    print(f".env       : {'存在' if env_file.exists() else '!! 缺失 !!'}  ({env_file})")
    dsn = os.environ.get("DATABASE_URL", "")
    print(f"DATABASE_URL: {mask_dsn(dsn) if dsn else '!! 未设置 !!'}")

    from api import pg_service

    try:
        pool = await pg_service._get_pool()
        async with pool.acquire() as conn:
            ver = await conn.fetchval("SELECT version()")
            await conn.fetchval("SELECT 1 FROM stock_daily_bars LIMIT 1")
        print(f"连接       : OK ({str(ver).split(',')[0]})")
        return True
    except Exception as e:
        print(f"连接       : !! 失败 !! {type(e).__name__}: {e}")
        print("  → 检查 .env 的 DATABASE_URL (asyncpg 口径), PostgreSQL 是否启动, 详见 README")
        return False


async def check_coverage(kinds: list[str]) -> dict:
    hr("2) 本地日线数据覆盖 (stock_daily_bars)")
    from api import pg_service

    info: dict = {}
    pool = await pg_service._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT coalesce(kind,'stock') AS kind, count(*) n, "
            "count(DISTINCT symbol) s, min(trade_date) mn, max(trade_date) mx "
            "FROM stock_daily_bars GROUP BY 1 ORDER BY 1")
    for r in rows:
        k = str(r["kind"])
        info[k] = {"n": int(r["n"]), "symbols": int(r["s"]),
                   "min": str(r["mn"]), "max": str(r["mx"])}
        mark = "" if k in kinds else "  (本次跳过)"
        print(f"  {k:6s} 行数={r['n']:>8}  标的={r['s']:>5}  "
              f"{r['mn']} ~ {r['mx']}{mark}")
    for k in kinds:
        if k not in info:
            print(f"  {k:6s} !! 无任何数据 !!")
    return info


async def check_targets(kinds: list[str], stale_days: int) -> list[dict]:
    hr(f"3) 目标标的 (我的股票 ∪ 策略Hub) 最新日检查 (落后 > {stale_days} 天视为异常)")
    from api import data_service, pg_service

    codes = await pg_service.my_and_strategy_codes()
    targets = []
    for c in codes:
        c = str(c)
        if c.upper().endswith(".HK"):
            kind = "hk"
        else:
            kind = "fund" if data_service._is_fund_code(c) else "stock"
        if kind in kinds:
            targets.append({"ts_code": c, "kind": kind})
    print(f"  目标共 {len(codes)} 只, 本次检查 {len(targets)} 只")

    if not targets:
        return []

    latest = await pg_service.latest_bar_dates([t["ts_code"] for t in targets])
    cutoff = (date.today() - timedelta(days=stale_days)).strftime("%Y%m%d")
    stale = []
    for t in targets:
        mx = latest.get(t["ts_code"])
        if not mx:
            stale.append({**t, "latest": None, "msg": "库中无数据"})
        elif mx < cutoff:
            gap = (date.today()
                   - datetime.strptime(mx, "%Y%m%d").date()).days
            stale.append({**t, "latest": mx, "msg": f"落后 {gap} 天"})
    if stale:
        print(f"  !! {len(stale)} 只落后 (前端会显示旧值或空白):")
        for s in stale[:40]:
            print(f"     {s['ts_code']:12s} kind={s['kind']:5s} "
                  f"本地最新={s['latest'] or '—'}  {s['msg']}")
        if len(stale) > 40:
            print(f"     ... 其余 {len(stale) - 40} 只省略")
    else:
        print("  OK: 全部标的的本地数据都是最新的")
    return stale


def check_cron() -> None:
    hr("4) 定时任务 (crontab) 与锁文件")
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=10)
        lines = [ln for ln in out.stdout.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if lines:
            print("  crontab 条目:")
            for ln in lines:
                print(f"    {ln}")
            has_nightly = any("nightly_update" in ln for ln in lines)
            has_sync = any("sync_target_daily" in ln for ln in lines)
            if not has_nightly and not has_sync:
                print("  !! 未发现 nightly_update / sync_target_daily 定时任务")
            else:
                if has_nightly:
                    print("  ✓ 发现 nightly_update (每日全量更新)")
                if has_sync:
                    print("  ✓ 发现 sync_target_daily (自选股日线增量)")
        else:
            print("  !! crontab 为空 (定时任务未安装) — 数据不会自动更新")
    except Exception as e:
        print(f"  读取 crontab 失败: {type(e).__name__}: {e}")

    lock = PROJECT_ROOT / "logs" / "nightly.lock"
    if lock.exists():
        pid = lock.read_text().strip()
        alive = False
        try:
            alive = subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0
        except Exception:
            pass
        flag = "运行中" if alive else "残留 (进程已结束, 不会阻塞下次运行)"
        print(f"  锁文件 {lock.name}: PID={pid} → {flag}")
    else:
        print("  无锁文件 (正常)")


def check_logs() -> None:
    hr("5) 最近一次 nightly 日志")
    logs = sorted((PROJECT_ROOT / "logs").glob("nightly_*.log"))
    if not logs:
        print("  !! 未找到 nightly_*.log — 定时任务可能从未成功执行")
        return
    latest = logs[-1]
    print(f"  日志: {latest.name}")
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  读取失败: {e}")
        return
    fails = [ln for ln in text.splitlines()
             if "!!!" in ln or ("失败" in ln and "失败 0" not in ln
                                and "失败:" in ln)]
    done = [ln for ln in text.splitlines() if ">>> [" in ln and "完成" in ln]
    print(f"  步骤完成标记 {len(done)} 条, 失败标记 {len(fails)} 条")
    for ln in fails[:15]:
        print(f"    {ln.strip()[:150]}")
    if not fails:
        print("  (无失败标记)")
    # 步骤是否只跑了开头几步 (慢步骤卡死会让后续步骤全部没执行)
    step_nums = []
    for ln in text.splitlines():
        if "步骤 " in ln and "/" in ln:
            try:
                # 形如 "[进度 50/2765]..." 或 "步骤 3/12"
                part = ln.split("步骤 ", 1)[1].split("/")[0].strip()
                step_nums.append(int(part))
            except (ValueError, IndexError):
                continue
    if step_nums:
        print(f"  已执行的最大步骤进度: {max(step_nums)} (完整应为 12)")
        if max(step_nums) <= 3:
            print("  !! 只执行了前几步 — 后面的步骤(含日线同步)根本没跑到, "
                  "通常是某步卡死/超时")
    # 常见根因提示
    errors = [ln for ln in text.splitlines()
              if "nodename nor servname" in ln
              or "Temporary failure in name resolution" in ln
              or "501" in ln or "waf.tencent" in ln or "Name or service not known" in ln]
    if errors:
        print(f"  !! 发现 {len(errors)} 条外部数据源/网络错误, 示例:")
        for ln in errors[:3]:
            print(f"     {ln.strip()[:140]}")
    slow = [ln for ln in text.splitlines() if "预计剩余" in ln]
    if slow:
        print(f"  进度行 {len(slow)} 条, 末条: {slow[-1].strip()[-70:]}")


async def run_fix(stale: list[dict], concurrency: int) -> None:
    hr("6) 补齐落后的日线数据 (--fix)")
    if not stale:
        print("  无需补数")
        return
    from api import data_service

    hk = [{"ts_code": s["ts_code"]} for s in stale if s["kind"] == "hk"]
    a = [{"ts_code": s["ts_code"], "kind": s["kind"]}
         for s in stale if s["kind"] != "hk"]

    if a:
        print(f"  A股/ETF {len(a)} 只 (tushare)...")
        res = await data_service.backfill_daily_bars(
            a, years=10, concurrency=concurrency, incremental=True, lookback_days=45)
        print(f"    ok={res['ok']} skip={res['skip']} rows={res['rows']}")
        for e in res["errors"][:10]:
            print(f"    !! {e['ts_code']}: {e['msg']}")

    if hk:
        print(f"  港股 {len(hk)} 只 (腾讯 K 线)...")
        res = await data_service.backfill_hk_daily_bars(
            hk, years=10, concurrency=concurrency, incremental=True, lookback_days=45)
        print(f"    ok={res['ok']} skip={res['skip']} rows={res['rows']}")
        for e in res["errors"][:10]:
            print(f"    !! {e['ts_code']}: {e['msg']}")
        if res["skip"]:
            print("    提示: 港股 skip 通常是腾讯接口取不到 (停牌/次新/网络), "
                  "已有历史数据仍会作为兜底返回")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="排查前端行情数据冻结在旧日期的问题 (线上部署首选工具)")
    parser.add_argument("--fix", action="store_true",
                        help="对落后的标的重新跑增量同步 (写库)")
    parser.add_argument("--days", type=int, default=7,
                        help="落后超过多少自然日算异常 (默认 7)")
    parser.add_argument("--kind", choices=["all", "stock", "fund", "hk"], default="all",
                        help="只检查某类标的 (默认 all)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="--fix 时的并发上限 (默认 4)")
    args = parser.parse_args()

    kinds = {"all": ["stock", "fund", "hk"]}.get(args.kind) or [args.kind]

    ok = await check_env()
    if not ok:
        return 2
    await check_coverage(kinds)
    stale = await check_targets(kinds, args.days)
    check_cron()
    check_logs()

    if args.fix:
        await run_fix(stale, args.concurrency)
    else:
        hr("结论")
        if stale:
            print(f"  发现 {len(stale)} 只标的的本地数据落后 → 加 --fix 可直接补齐")
        else:
            print("  本地数据正常。若前端仍显示旧日期, 请检查:")
            print("    1) 后端是否已用最新代码重启 (uvicorn 进程启动时间)")
            print("    2) 浏览器缓存 (静态资源 ?v= 版本号)")
            print("    3) 前端是否经反代/不同实例连到另一套数据库")
        print("  提示: 本脚本默认只读, 加 --fix 才会写库。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))