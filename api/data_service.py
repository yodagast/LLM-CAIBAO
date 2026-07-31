"""tushare 数据服务层。

职责:
  - 从 ../.env 读取 TUSHARE_TOKEN 并初始化 tushare pro 接口
  - 股票代码解析: 6 位代码 → ts_code + 名称 (支持股票 / ETF)
  - 日线数据获取 (股票 daily / ETF fund_daily), 带内存缓存
  - 股票关键字搜索 (前端联想)

约定 (与 strategy/ 下脚本一致):
  - token 从项目根目录 .env 读取 (TUSHARE_TOKEN)
"""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
import tushare as ts

# 项目根目录 (api/ 的上一级)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_token() -> None:
    """加载 ../.env 中的环境变量 (若未设置)。"""
    if os.getenv("TUSHARE_TOKEN"):
        return
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def _init_pro():
    _load_env_token()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置 TUSHARE_TOKEN, 请在项目根目录 .env 中配置。")
    ts.set_token(token)
    return ts.pro_api(token)


# ---------------------------------------------------------------------------
# 股票基本信息 / 代码解析
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _stock_basic() -> pd.DataFrame:
    """获取全部上市股票基本信息 (缓存)。"""
    pro = _init_pro()
    return pro.stock_basic(exchange="", list_status="L",
                           fields="ts_code,symbol,name,industry,market,list_date")


@lru_cache(maxsize=1)
def _fund_basic() -> pd.DataFrame:
    """获取全部上市基金基本信息 (缓存, 用于 ETF 解析)。"""
    pro = _init_pro()
    try:
        df = pro.fund_basic(market="E")
    except Exception:
        df = pd.DataFrame(columns=["ts_code", "name"])
    # fund_basic 无 symbol 列, 由 ts_code 推导 (如 513050.SH → 513050)
    if "symbol" not in df.columns:
        df["symbol"] = df["ts_code"].str.split(".").str[0]
    return df


def resolve_code(code: str) -> dict:
    """解析股票/ETF 代码, 返回 {"ts_code", "symbol", "name", "kind"}。

    支持两种输入:
      - 6 位数字代码, 如 "000858" → "000858.SZ" (五粮液)
      - 带后缀代码, 如 "000858.SZ" / "513050.SH"
    """
    code = code.strip().upper()
    if not code:
        raise ValueError("股票代码不能为空。")

    # 规范输入: 去掉空格等
    ts_code = code if "." in code else None

    stocks = _stock_basic()
    funds = _fund_basic()

    # 1) 6 位数字代码: 在股票 / 基金表中查找
    if ts_code is None:
        hit = stocks[stocks["symbol"] == code]
        if not hit.empty:
            row = hit.iloc[0]
            return {"ts_code": row["ts_code"], "symbol": code, "name": row["name"], "kind": "stock"}
        hit_f = funds[funds["symbol"] == code]
        if not hit_f.empty:
            row = hit_f.iloc[0]
            return {"ts_code": row["ts_code"], "symbol": code, "name": row["name"], "kind": "fund"}
        raise ValueError(f"未找到代码 [{code}] 对应的股票/基金。")

    # 2) 带后缀代码: 校验存在性
    hit = stocks[stocks["ts_code"] == ts_code]
    if not hit.empty:
        row = hit.iloc[0]
        return {"ts_code": ts_code, "symbol": row["symbol"], "name": row["name"], "kind": "stock"}
    hit_f = funds[funds["ts_code"] == ts_code]
    if not hit_f.empty:
        row = hit_f.iloc[0]
        return {"ts_code": ts_code, "symbol": row["symbol"], "name": row["name"], "kind": "fund"}
    raise ValueError(f"未找到代码 [{ts_code}] 对应的股票/基金。")


def search_stock(keyword: str, limit: int = 20) -> list[dict]:
    """按代码或名称模糊搜索股票, 供前端联想使用。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    stocks = _stock_basic()
    mask = (
        stocks["symbol"].str.contains(keyword, na=False)
        | stocks["ts_code"].str.contains(keyword, na=False)
        | stocks["name"].str.contains(keyword, na=False)
    )
    hits = stocks[mask].head(limit)
    return [
        {"ts_code": row["ts_code"], "symbol": row["symbol"], "name": row["name"]}
        for _, row in hits.iterrows()
    ]


# ---------------------------------------------------------------------------
# 日线数据 (带缓存)
# ---------------------------------------------------------------------------

_DAILY_CACHE: dict[str, pd.DataFrame] = {}


def get_daily(ts_code: str, kind: str = "stock",
              start_date: str = "20170101", end_date: str = "") -> pd.DataFrame:
    """获取单只股票/ETF 日线 (trade_date 升序), 带内存缓存。"""
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    cache_key = f"{ts_code}:{kind}:{start_date}:{end_date}"
    if cache_key in _DAILY_CACHE:
        return _DAILY_CACHE[cache_key]

    pro = _init_pro()
    df = pd.DataFrame()
    if kind == "fund" or ts_code.startswith(("51", "50", "15", "16", "18")):
        try:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception:
            df = pd.DataFrame()
        if df.empty:
            df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    else:
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df.empty:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

    if df is None or df.empty:
        raise ValueError(f"未获取到 {ts_code} 在 {start_date}~{end_date} 的日线数据。")

    df = df.sort_values("trade_date").reset_index(drop=True)
    _DAILY_CACHE[cache_key] = df
    return df
