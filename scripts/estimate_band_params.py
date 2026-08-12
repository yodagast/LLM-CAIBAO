#!/usr/bin/env python3
"""区间交易参数估算脚本 (复用 api/band_service.optimize_band 的估算逻辑)。

用法:
  python scripts/estimate_band_params.py --code 600036            # 单只股票
  python scripts/estimate_band_params.py --code 512170            # 基金/ETF (自动识别)
  python scripts/estimate_band_params.py --codes 600036,000858,510050
  python scripts/estimate_band_params.py --industry 白酒          # 行业全部股票
  python scripts/estimate_band_params.py --industry 银行 --limit 20
  python scripts/estimate_band_params.py --objective drawdown --min-sharpe 1.2 --start 2020 --end 2025
  python scripts/estimate_band_params.py --sort sharpe --order desc   # 结果排序 (默认按夏普降序)
  python scripts/estimate_band_params.py --csv band_params.csv    # 保存结果 CSV

说明:
  - 复用区间交易 tab 的参数估算 (前复权价 + T+1 + 剔除买入价=卖出价的无效率交易)
  - 对每只标的输出最优 买入价/卖出价/止损价 及相关指标 (总收益/年化/回撤/夏普/卡玛/交易笔数/是否达标)
  - 支持股票 / 基金(ETF) / 行业批量; 结果可存 CSV
"""

import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import band_service, data_service  # noqa: E402
from scripts.download_annual_reports import _load_env  # noqa: E402

CSV_HEADERS = [
    "name", "ts_code", "kind",
    "buy_price", "sell_price", "stop_price",
    "total_return_pct", "annual_return_pct", "max_drawdown_pct",
    "sharpe", "calmar", "trades", "max_trades", "achieved",
    "start", "end",
]


async def _industry_stocks(industry: str, limit: int = 0) -> list:
    """按东财行业分类子串匹配获取股票列表 (与基本面选股一致), 返回 [(ts_code, name)]。"""
    df = await data_service._stock_basic()
    if industry:
        df = df[df["industry"].fillna("").astype(str).str.contains(industry, na=False)]
    df = df.sort_values("ts_code")
    if limit and limit > 0:
        df = df.head(limit)
    return [(str(r["ts_code"]), str(r["name"])) for _, r in df.iterrows()]


async def estimate_one(ts_code: str, capital: float, start_date: str, end_date: str,
                       objective: str, min_sharpe: float, max_trades: int | None = 100,
                       sleep: float = 0.0) -> dict:
    """估算单只股票/基金的最优区间交易参数与指标。"""
    info = await data_service.resolve_code(ts_code)
    df = await data_service.get_daily(info["ts_code"], kind=info["kind"],
                                      start_date=start_date, end_date=end_date, adj="qfq")
    if sleep and sleep > 0:
        await asyncio.sleep(sleep)
    r = await asyncio.to_thread(band_service.optimize_band, df, capital=capital,
                                min_sharpe=min_sharpe, objective=objective,
                                max_trades=max_trades)
    p, m, s, trades = r["params"], r["band"]["metrics"], r["search"], r["trades"]
    return {
        "name": info.get("name", ts_code),
        "ts_code": info["ts_code"],
        "kind": info.get("kind", "stock"),
        "buy_price": p["buy_price"],
        "sell_price": p["sell_price"],
        "stop_price": p["stop_price"],
        "total_return_pct": round(m["total_return"], 2),
        "annual_return_pct": round(m["annual_return"], 2),
        "max_drawdown_pct": round(m["max_drawdown"], 2),
        "sharpe": round(m["sharpe"], 2),
        "calmar": round(m["calmar"], 2),
        "trades": len(trades),
        "max_trades": max_trades,
        "achieved": s["achieved"],
        "start": r["range"]["start"],
        "end": r["range"]["end"],
    }


def print_row(row: dict) -> None:
    print(f"  {row['name']:<8} {row['ts_code']:<11} [{row['kind']:<5}] "
          f"买 {row['buy_price']:>8.2f} 卖 {row['sell_price']:>8.2f} 损 {row['stop_price']:>8.2f} | "
          f"收益 {row['total_return_pct']:>7.2f}% 年化 {row['annual_return_pct']:>6.2f}% "
          f"回撤 {row['max_drawdown_pct']:>6.2f}% 夏普 {row['sharpe']:>5.2f} 卡玛 {row['calmar']:>5.2f} "
          f"交易 {row['trades']:>3} 达标={'✅' if row['achieved'] else '❌'}")


def _sort_rows(rows: list, sort: str, order: str) -> list:
    """按指定字段排序结果 (数值/字符串/布尔自适应, order=asc/desc)。"""
    if not sort:
        return rows

    def key(r: dict):
        v = r.get(sort)
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return v
        return str(v)

    return sorted(rows, key=key, reverse=(order == "desc"))


async def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="区间交易参数估算 (复用区间交易 tab 逻辑)")
    parser.add_argument("--code", default="", help="单只股票/基金代码 (如 600036 / 512170)")
    parser.add_argument("--codes", default="", help="多个代码, 逗号分隔 (如 600036,000858,510050)")
    parser.add_argument("--industry", default="", help="行业名称 (东财分类), 空=不按行业")
    parser.add_argument("--limit", type=int, default=0, help="行业最多处理股票数 (0=不限)")
    parser.add_argument("--start", default="20170101", help="历史起始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="历史结束日期 YYYYMMDD, 空=最新")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金")
    parser.add_argument("--objective", default="balanced",
                        help="优化目标: balanced/return/annual/sharpe/drawdown/calmar")
    parser.add_argument("--min-sharpe", type=float, default=1.0, help="目标夏普下限")
    parser.add_argument("--max-trades", type=int, default=100,
                        help="交易次数上限 (每笔=买入→卖出完整周期, 超限参数被淘汰; 默认100, 0=不限)")
    parser.add_argument("--csv", default="", help="结果 CSV 保存路径 (如 band_params.csv)")
    parser.add_argument("--sleep", type=float, default=0.0, help="每只标的间隔秒数")
    parser.add_argument("--sort", default="",
                        help="结果排序字段: " + ",".join(CSV_HEADERS) + " (如 sharpe/total_return_pct); 空=不排序")
    parser.add_argument("--order", default="desc", choices=["asc", "desc"],
                        help="排序方向 (默认 desc, 降序)")
    args = parser.parse_args()

    # max-trades: 0 表示不限制
    max_trades = args.max_trades if args.max_trades and args.max_trades > 0 else None

    # 标的来源
    if args.code:
        stocks = [(args.code, args.code)]
    elif args.codes:
        stocks = [(c.strip(), c.strip()) for c in args.codes.split(",") if c.strip()]
    elif args.industry:
        stocks = await _industry_stocks(args.industry, args.limit)
    else:
        print("请指定 --code / --codes / --industry 之一。")
        return

    print(f"区间交易参数估算: {len(stocks)} 个标的, 区间 {args.start}~{args.end or '最新'}, "
          f"目标 {args.objective}, 目标夏普≥{args.min_sharpe}, "
          f"最大交易≤{max_trades if max_trades else '不限'}\n")

    results = []
    t0 = time.time()
    for i, (ts_code, _name) in enumerate(stocks, 1):
        try:
            row = await estimate_one(ts_code, args.capital, args.start, args.end,
                                     args.objective, args.min_sharpe, max_trades, args.sleep)
            results.append(row)
        except Exception as e:
            print(f"  ✗ {ts_code} 估算失败: {e}")
        print(f"  [{i}/{len(stocks)}] 完成, 累计 {(time.time() - t0):.0f}s")

    # 排序 (输出 + CSV 一致)
    if args.sort:
        results = _sort_rows(results, args.sort, args.order)
        print(f"已按 {args.sort} {args.order} 排序")

    # 输出 (按排序后顺序)
    for row in results:
        print_row(row)

    # 汇总
    ok = sum(1 for r in results if r["achieved"])
    print(f"\n完成: {len(results)}/{len(stocks)} 成功, 其中 {ok} 个达标(夏普≥{args.min_sharpe}), "
          f"耗时 {time.time() - t0:.0f}s")

    # CSV
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"结果已保存: {args.csv}")


if __name__ == "__main__":
    asyncio.run(main())
