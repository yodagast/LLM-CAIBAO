#!/usr/bin/env python3
"""
计算某股票最近N个交易日的波动率。

支持命令行使用和模块导入两种方式：

命令行:
    python calculate_volatility.py <ts_code> [--days N] [--annualize]

示例:
    python calculate_volatility.py 000858.SZ
    python calculate_volatility.py 000858.SZ --days 5
    python calculate_volatility.py 000858.SZ --days 20 --annualize

模块导入:
    from calculate_volatility import calculate_volatility, VolatilityResult

    result = calculate_volatility("000858.SZ", days=5)
    print(result.volatility_3d)
"""

import os
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import tushare as ts


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _load_env_token(env_path: str = ".env") -> None:
    """从 .env 文件中加载 TUSHARE_TOKEN 到环境变量。"""
    if os.getenv("TUSHARE_TOKEN"):
        return
    resolved = os.path.join(os.path.dirname(os.path.abspath(__file__)), env_path)
    if os.path.exists(resolved):
        with open(resolved) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    os.environ[key] = value


def _init_pro() -> ts.pro_api:
    """初始化并返回 Tushare Pro API 实例。"""
    _load_env_token()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError(
            "未设置 TUSHARE_TOKEN。请在 .env 文件中配置，或通过环境变量设置。"
        )
    ts.set_token(token)
    return ts.pro_api(token)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class VolatilityResult:
    """波动率计算结果。"""
    ts_code: str                  # 股票代码
    days: int                     # 计算所用的交易日数
    trade_dates: list[str]        # 交易日列表（升序）
    closes: list[float]           # 对应收盘价
    pct_changes: list[float]      # 对应涨跌幅（百分比）
    volatility_daily: float       # 日波动率（百分比）
    volatility_annualized: float  # 年化波动率（百分比）
    max_gain: float               # 期间最大涨幅（百分比）
    max_loss: float               # 期间最大跌幅（百分比）
    avg_change: float             # 期间平均涨跌幅（百分比）

    def __str__(self) -> str:
        sep = "=" * 52
        lines = [
            f"\n{sep}",
            f"  {self.ts_code}  最近 {self.days} 天波动率分析",
            f"{sep}",
            f"  交易日范围: {self.trade_dates[0]} ~ {self.trade_dates[-1]}",
            f"  日波动率:   {self.volatility_daily:.4f}%",
            f"  年化波动率: {self.volatility_annualized:.2f}%",
            f"  最大涨幅:   {self.max_gain:+.2f}%",
            f"  最大跌幅:   {self.max_loss:+.2f}%",
            f"  平均涨跌幅: {self.avg_change:+.2f}%",
            f"{sep}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------

def calculate_volatility(
    ts_code: str,
    days: int = 3,
    annualize: bool = True,
    trading_days_per_year: int = 252,
) -> VolatilityResult:
    """
    计算某股票最近 N 个交易日的波动率。

    参数:
        ts_code:       Tushare 股票代码，如 "000858.SZ" 或 "600519.SH"
        days:          计算所用的最近交易日数量（默认 3）
        annualize:     是否计算年化波动率（默认 True）
        trading_days_per_year: 年化时使用的年交易日数（默认 252）

    返回:
        VolatilityResult 对象，包含波动率及统计信息。

    抛出:
        ValueError: 数据不足时抛出。
    """
    if days < 2:
        raise ValueError("days 必须 >= 2，至少需要 2 个交易日才能计算波动率。")

    pro = _init_pro()

    # 查询足够多的历史数据
    today = datetime.now()
    # 保守估计每天约 1 条数据，多取一些防止停牌
    lookback_days = max(days * 3, 30)
    start_date = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

    if df.empty:
        # 扩大查询范围再试一次
        start_date = (today - timedelta(days=180)).strftime("%Y%m%d")
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

    if df.empty:
        raise ValueError(f"未获取到 {ts_code} 的任何日线数据，请检查股票代码是否正确。")

    # 按日期升序排列
    df = df.sort_values("trade_date").reset_index(drop=True)

    if len(df) < days:
        raise ValueError(
            f"最近 {lookback_days} 天内仅获取到 {len(df)} 条交易数据，"
            f"不足以计算 {days} 天的波动率。"
        )

    recent = df.tail(days).copy()

    trade_dates = recent["trade_date"].tolist()
    closes = recent["close"].tolist()
    pct_changes = recent["pct_chg"].tolist()

    # 涨跌幅（百分比）转小数后计算标准差
    returns = np.array(pct_changes) / 100.0
    volatility_daily = float(np.std(returns, ddof=1)) * 100

    volatility_annualized = volatility_daily * np.sqrt(trading_days_per_year) if annualize else 0.0

    return VolatilityResult(
        ts_code=ts_code,
        days=days,
        trade_dates=trade_dates,
        closes=closes,
        pct_changes=pct_changes,
        volatility_daily=volatility_daily,
        volatility_annualized=volatility_annualized,
        max_gain=max(pct_changes),
        max_loss=min(pct_changes),
        avg_change=float(np.mean(pct_changes)),
    )


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="计算某股票最近 N 个交易日的波动率。"
    )
    parser.add_argument(
        "ts_code",
        type=str,
        help='股票代码，例如 "000858.SZ" 或 "600519.SH"',
    )
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=3,
        help="计算所用的最近交易日数量（默认 3，至少 2）",
    )
    parser.add_argument(
        "--annualize",
        "-a",
        action="store_true",
        default=True,
        help="同时计算年化波动率（默认开启）",
    )
    parser.add_argument(
        "--no-annualize",
        action="store_true",
        help="关闭年化波动率计算",
    )

    args = parser.parse_args()

    # --no-annualize 优先级高于 --annualize
    final_annualize = not args.no_annualize if args.no_annualize else args.annualize

    try:
        result = calculate_volatility(
            ts_code=args.ts_code,
            days=args.days,
            annualize=final_annualize,
        )
        print(result)

        # 额外打印明细
        print("\n  交易日明细:")
        print(f"  {'日期':<12} {'收盘价':>8} {'涨跌幅':>8}")
        print(f"  {'-'*30}")
        for date, close, pct in zip(
            result.trade_dates, result.closes, result.pct_changes
        ):
            print(f"  {date:<12} {close:>8.2f} {pct:>+7.2f}%")

    except (ValueError, RuntimeError) as e:
        print(f"[错误] {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
