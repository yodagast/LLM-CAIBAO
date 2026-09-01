"""ETF 筛选服务: 基于 tushare 基金数据计算关键指标 (费用/规模/流动性/折溢价/跟踪/52周)。

数据来源:
  fund_basic   → 名称/类型/成立日/上市日/管理费率/托管费率
  fund_daily   → 收盘/涨跌/成交额/成交量/52周高低 (场内价格, amount 单位千元, vol 单位手)
  fund_nav     → 单位净值 unit_nav (折溢价、规模、跟踪偏离)
  fund_share   → 基金份额 fd_share (万份; 规模 = 份额 × 净值)

指标口径:
  规模(亿元)       = 最新份额(万份) × 最新单位净值(元) / 10000
  折溢价(%)        = (收盘价 − 单位净值) / 单位净值 × 100    (正=溢价 场内贵于净值)
  日均成交额(万元) = fund_daily.amount(千元) / 10, 近 N 日均值
  52周位置          = (现价 − 52周最低) / (52周最高 − 52周最低), 0~1 (越高越接近高位)
  跟踪偏离度(%)     = mean(|价格日收益 − 净值日收益|) × 100, 近 20 个共同交易日 (衡量跟踪效果)
  存续年限          = 上市日至今 (年)
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pandas as pd

from . import data_service

# 单只 ETF 的 daily/nav 缓存 (15 分钟 TTL), 避免重复打 tushare
_ETF_CACHE: dict[str, tuple[float, dict]] = {}
# 全市场 份额/净值 批量缓存 (15 分钟)
_BATCH_CACHE: dict[str, tuple[float, dict, dict]] = {}
_ETF_TTL = 15 * 60

# 默认取近 250 个交易日作为 52 周窗口; 抓取起点按 1.6 倍日历日 + 缓冲
_DAYS_52W = 250
_START_BUFFER_DAYS = 400


def _num(v) -> float | None:
    """安全转 float, NaN/None → None。"""
    if v is None:
        return None
    try:
        f = float(v)
        return f if not pd.isna(f) else None
    except (TypeError, ValueError):
        return None


def _init_pro():
    return data_service._init_pro()


async def etf_list() -> list[dict]:
    """场内 ETF/基金列表 (已上市未退市), 含费率/类型/日期等基础信息。"""
    pro = _init_pro()
    df = await pro.fund_basic(market="E", fields=(
        "ts_code,name,management,fund_type,found_date,list_date,delist_date,m_fee,c_fee,status"))
    if df is None or df.empty:
        return []
    # 仅保留已上市 (有上市日) 且未退市
    df = df[df["list_date"].notna() & (df["status"].fillna("") != "D")]
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "ts_code": str(r["ts_code"]),
            "name": str(r.get("name") or ""),
            "fund_type": str(r.get("fund_type") or ""),
            "found_date": str(r.get("found_date") or "") or "",
            "list_date": str(r.get("list_date") or "") or "",
            "m_fee": _num(r.get("m_fee")),
            "c_fee": _num(r.get("c_fee")),
            "management": str(r.get("management") or ""),
        })
    return rows


async def _probe_trade_date() -> str:
    """探测最新交易日 (轻量, 用 000001.SZ 日线)。"""
    pro = _init_pro()
    try:
        d = await pro.daily(ts_code="000001.SZ",
                            start_date=(datetime.now() - timedelta(days=10)).strftime("%Y%m%d"),
                            end_date="")
        if d is not None and not d.empty:
            return str(d["trade_date"].iloc[0])
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")


async def _batch_share_nav() -> tuple[dict, dict]:
    """批量获取最新交易日全市场基金 份额 map 与 净值 map (各 1 次 tushare 调用, 带缓存)。"""
    pro = _init_pro()
    now = time.time()
    hit = _BATCH_CACHE.get("latest")
    if hit and now - hit[0] < _ETF_TTL:
        return hit[1], hit[2]
    td = await _probe_trade_date()
    share_map: dict[str, float] = {}
    nav_map: dict[str, float] = {}
    try:
        fs = await pro.fund_share(trade_date=td)
        if fs is not None and not fs.empty:
            share_map = {str(r["ts_code"]): float(r["fd_share"]) for _, r in fs.iterrows()}
    except Exception:
        pass
    try:
        fn = await pro.fund_nav(nav_date=td)
        if fn is not None and not fn.empty:
            nav_map = {str(r["ts_code"]): float(r["unit_nav"]) for _, r in fn.iterrows()}
    except Exception:
        pass
    _BATCH_CACHE["latest"] = (now, share_map, nav_map)
    return share_map, nav_map


async def _fetch_cached(ts_code: str, start_date: str) -> dict | None:
    """获取并缓存单只 ETF 的 fund_daily + fund_nav 历史序列 (近一年)。"""
    now = time.time()
    hit = _ETF_CACHE.get(ts_code)
    if hit and now - hit[0] < _ETF_TTL:
        return hit[1]
    pro = _init_pro()
    try:
        daily = await pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date="")
        if daily is None or daily.empty:
            return None
        nav = await pro.fund_nav(ts_code=ts_code, start_date=start_date, end_date="")
    except Exception:
        return None
    data = {"daily": daily, "nav": nav}
    _ETF_CACHE[ts_code] = (now, data)
    return data


def _compute_metrics(info: dict, data: dict,
                     unit_nav_override: float | None = None,
                     fd_share_override: float | None = None) -> dict:
    """由基础信息 + daily/nav 计算全部筛选指标 (份额/最新净值可走批量缓存覆盖)。"""
    daily = data["daily"].copy().sort_values("trade_date")
    last = daily.iloc[-1]
    close = float(last["close"])
    pct_chg = _num(last.get("pct_chg"))
    recent = daily.tail(_DAYS_52W)
    # 52 周高低改用**月线**计算 (最近 12 个月月线 high/low 极值, 日线兜底)
    monthly_52w = data_service._monthly_52week_high_low(daily)
    if monthly_52w:
        high52, low52 = monthly_52w
    else:
        high52 = float(recent["high"].max())
        low52 = float(recent["low"].min())
    amt = daily["amount"].astype(float)
    avg_amount_20 = float(amt.tail(20).mean()) / 10          # 千元 → 万元
    avg_amount_5 = float(amt.tail(5).mean()) / 10
    avg_vol_20 = float(daily["vol"].astype(float).tail(20).mean())  # 手

    # 净值 (优先批量最新净值, 其次历史序列最新)
    unit_nav = unit_nav_override
    nav = data.get("nav")
    nav_series = None
    if nav is not None and not nav.empty:
        nav = nav.sort_values("nav_date")
        nav_series = nav
        if unit_nav is None:
            unit_nav = _num(nav.iloc[-1].get("unit_nav"))

    # 份额 → 规模 (优先批量)
    fd_share = fd_share_override
    scale = None
    if fd_share is not None and unit_nav:
        scale = fd_share * unit_nav / 10000.0               # 万份 × 元 / 10000 = 亿元
    premium = None
    if unit_nav:
        premium = (close - unit_nav) / unit_nav * 100.0
        # 货币/债券 ETF 净值口径可能为 100 元面值或个别异常历史值, |折溢价|>50% 视为数据口径异常
        if abs(premium) > 50:
            premium = None

    # 52 周位置
    pos52 = None
    if high52 > low52:
        pos52 = (close - low52) / (high52 - low52)

    # 跟踪偏离度 (近 20 个共同交易日: |价格日收益 − 净值日收益| 均值)
    track_dev = None
    try:
        if nav_series is not None and not nav_series.empty:
            nd = nav_series[["nav_date", "unit_nav"]].dropna()
            dd = daily[["trade_date", "close"]].dropna()
            m = pd.merge(dd, nd, left_on="trade_date", right_on="nav_date", how="inner")
            if len(m) >= 3:
                m = m.sort_values("trade_date").tail(20)
                p_ret = m["close"].pct_change()
                n_ret = m["unit_nav"].pct_change()
                track_dev = float((p_ret - n_ret).abs().mean() * 100.0)
    except Exception:
        track_dev = None

    # 存续年限 (上市日至今)
    age = None
    if info.get("list_date"):
        try:
            ld = datetime.strptime(info["list_date"], "%Y%m%d")
            age = round((datetime.now() - ld).days / 365.25, 1)
        except Exception:
            age = None

    m_fee = info.get("m_fee")
    c_fee = info.get("c_fee")
    total_fee = None
    if m_fee is not None or c_fee is not None:
        total_fee = round((m_fee or 0) + (c_fee or 0), 3)

    return {
        "ts_code": info["ts_code"],
        "name": info["name"],
        "fund_type": info.get("fund_type", ""),
        "management": info.get("management", ""),
        "found_date": info.get("found_date", ""),
        "list_date": info.get("list_date", ""),
        "age_years": age,
        "close": round(close, 4),
        "pct_chg": round(pct_chg, 2) if pct_chg is not None else None,
        "unit_nav": round(unit_nav, 4) if unit_nav is not None else None,
        "fd_share": round(fd_share / 10000.0, 2) if fd_share is not None else None,  # 亿份
        "scale": round(scale, 2) if scale is not None else None,                     # 亿元
        "m_fee": m_fee,
        "c_fee": c_fee,
        "total_fee": total_fee,
        "avg_amount_20": round(avg_amount_20, 0),
        "avg_amount_5": round(avg_amount_5, 0),
        "avg_vol_20": round(avg_vol_20, 0),
        "premium": round(premium, 3) if premium is not None else None,
        "track_dev": round(track_dev, 3) if track_dev is not None else None,
        "high52": round(high52, 4),
        "low52": round(low52, 4),
        "pos52": round(pos52, 4) if pos52 is not None else None,
    }


async def sync_all(limit: int = 0, refresh: bool = False, sleep: float = 0.0,
                   batch: int = 100) -> dict:
    """全市场 ETF 指标初始化: 计算并 upsert 到 PostgreSQL etf_screen 表 (夜间脚本调用)。

    limit: 0=全部已上市 ETF (约2700只, 每只约 2 次 tushare 调用); 幂等 upsert 可断点续跑。
    前端 screen_etfs 检测到 DB 有数据后改为读库 (不再实时计算)。
    """
    from . import pg_service
    await pg_service.init_etf_schema()
    if refresh:
        _ETF_CACHE.clear()
        _BATCH_CACHE.clear()
    rows = await etf_list()
    # 预排序 (规模代理): 先算规模大的
    share_map, _ = await _batch_share_nav()
    for r in rows:
        r["_scale_proxy"] = share_map.get(r["ts_code"])
    rows.sort(key=lambda r: (r["_scale_proxy"] is not None, r["_scale_proxy"] or 0),
              reverse=True)
    if limit and limit > 0:
        rows = rows[:limit]

    start = (datetime.now() - timedelta(days=_START_BUFFER_DAYS)).strftime("%Y%m%d")
    calc_date = await _probe_trade_date()
    items: list[dict] = []
    ok = fail = 0
    stored = 0
    for i, info in enumerate(rows, 1):
        data = await _fetch_cached(info["ts_code"], start)
        if data is None:
            fail += 1
            continue
        try:
            m = _compute_metrics(info, data,
                                 fd_share_override=share_map.get(info["ts_code"]))
        except Exception:
            fail += 1
            continue
        m["calc_date"] = calc_date
        items.append(m)
        ok += 1
        if sleep > 0:
            await asyncio.sleep(sleep)
        if batch and batch > 0 and len(items) >= batch:
            stored += await pg_service.upsert_etf_rows(items)
            items = []
        if i % 200 == 0:
            print(f"  已处理 {i}/{len(rows)} ...")
    if items:
        stored += await pg_service.upsert_etf_rows(items)
    return {"total": len(rows), "ok": ok, "fail": fail,
            "stored": stored, "calc_date": calc_date}


async def screen_etfs(keyword: str = "", fund_type: str = "",
                      min_scale: float | None = None, max_m_fee: float | None = None,
                      max_c_fee: float | None = None, min_amount_20: float | None = None,
                      max_premium: float | None = None, min_pos52: float | None = None,
                      max_pos52: float | None = None,
                      sort_by: str = "scale", order: str = "desc",
                      limit: int = 300, refresh: bool = False) -> dict:
    """按条件筛选场内 ETF, 计算关键指标并按字段排序。

    优先读取 PostgreSQL etf_screen 表 (夜间初始化脚本 scripts/init_etf.py 产出,
    覆盖全市场); 无数据时回退实时计算 (逐只抓取 daily/nav, 带缓存)。
    """
    from . import pg_service
    if refresh:
        _ETF_CACHE.clear()
        _BATCH_CACHE.clear()

    # 1) DB 优先: 已有初始化数据则直接读库 (快, 且覆盖全市场)
    db_date = await pg_service.latest_etf_calc_date()
    if db_date:
        items = await pg_service.query_etf(
            calc_date=db_date, keyword=keyword, fund_type=fund_type,
            min_scale=min_scale, max_m_fee=max_m_fee, max_c_fee=max_c_fee,
            min_amount_20=min_amount_20, max_premium=max_premium,
            min_pos52=min_pos52, max_pos52=max_pos52,
            sort_by=sort_by, order=order, limit=limit)
        return {
            "count": len(items), "items": items,
            "ok": len(items), "fail": 0,
            "total": await pg_service.count_etf_by_calc_date(db_date),
            "source": "db", "calc_date": db_date,
            "limit": limit, "sort_by": sort_by, "order": order,
            "fund_type": fund_type, "keyword": keyword,
            "cached": True,
        }

    # 2) 兜底: 实时计算 (初始化前)
    rows = await etf_list()

    # 基础过滤 (成本信息来自 fund_basic, 无需抓行情)
    if keyword:
        kw = keyword.strip().lower()
        rows = [r for r in rows
                if kw in r["ts_code"].lower() or kw in r["name"].lower()]
    if fund_type:
        rows = [r for r in rows if fund_type in (r.get("fund_type") or "")]
    if max_m_fee is not None:
        rows = [r for r in rows if r["m_fee"] is not None and r["m_fee"] <= max_m_fee]
    if max_c_fee is not None:
        rows = [r for r in rows if r["c_fee"] is not None and r["c_fee"] <= max_c_fee]

    # 批量份额 → 规模代理 (份额×均价近似, 只用于预排序), 规模大的在前
    share_map, _ = await _batch_share_nav()
    for r in rows:
        r["_scale_proxy"] = share_map.get(r["ts_code"])
    rows.sort(key=lambda r: (r["_scale_proxy"] is not None, r["_scale_proxy"] or 0),
              reverse=True)

    start = (datetime.now() - timedelta(days=_START_BUFFER_DAYS)).strftime("%Y%m%d")
    items: list[dict] = []
    ok = fail = 0
    total = len(rows)
    scan_cap = min(len(rows), max(limit, 1))
    for info in rows[:scan_cap]:
        if len(items) >= limit:
            break
        data = await _fetch_cached(info["ts_code"], start)
        if data is None:
            fail += 1
            continue
        try:
            m = _compute_metrics(info, data,
                                 fd_share_override=share_map.get(info["ts_code"]))
        except Exception:
            fail += 1
            continue

        # 数值过滤 (需行情/净值数据)
        if min_scale is not None and (m["scale"] is None or m["scale"] < min_scale):
            continue
        if min_amount_20 is not None and (m["avg_amount_20"] is None or m["avg_amount_20"] < min_amount_20):
            continue
        if max_premium is not None and (m["premium"] is None or abs(m["premium"]) > max_premium):
            continue
        if min_pos52 is not None and (m["pos52"] is None or m["pos52"] < min_pos52):
            continue
        if max_pos52 is not None and (m["pos52"] is None or m["pos52"] > max_pos52):
            continue

        items.append(m)
        ok += 1

    # 排序 (None 值排最后)
    desc = (order == "desc")
    if desc:
        items.sort(key=lambda x: (x.get(sort_by) is not None, x.get(sort_by) or 0),
                   reverse=True)
    else:
        items.sort(key=lambda x: (x.get(sort_by) is None, x.get(sort_by) or 0))

    return {
        "count": len(items),
        "items": items,
        "total": total,
        "ok": ok,
        "fail": fail,
        "limit": limit,
        "sort_by": sort_by,
        "order": order,
        "fund_type": fund_type,
        "keyword": keyword,
        "source": "live",
        "cached": not refresh,
    }
