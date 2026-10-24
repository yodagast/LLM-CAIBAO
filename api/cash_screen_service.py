"""现金选股服务: 按「账面现金 ÷ 总市值」计算现金市值比并排序筛选 (A股 / 港股)。

口径:
  - 账面现金: A股 = financial_data.money_cap (最新财年, 亿元);
              港股 = hk_fundamental_screen.money_cap (最新财年, 万港元)
  - 总市值  : A股 = 当日 daily_basic.total_mv (万元, 实时);
              港股 = 财年末 total_mv (万港元) × 最新收盘 / 财年末收盘 (折算到最新市值)
  - 现金市值比 % = 账面现金 / 总市值 × 100 (统一换算为 万元 后计算)
  - 输出金额统一为 万元 / 万港元, 前端 fmtYi() 直接渲染为 亿

全链路批量 (1 次 SQL + 批量行情), 无逐只请求; 结果带 5 分钟进程内缓存。
"""

from __future__ import annotations

import time

from . import data_service as ds
from . import hk_data_service as hkd
from . import pg_service as pg

# 结果缓存 (key = market:industry), 5 分钟 TTL, 避免反复读库 + 打行情接口
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300

# 支持的阈值字段 (其余字段忽略, 防 SQL/逻辑注入)
_FILTER_KEYS = ("cash_mv_ratio", "total_mv", "roe", "gross_margin")


def _cache_get(key: str) -> list[dict] | None:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _cache_put(key: str, items: list[dict]) -> None:
    if len(_CACHE) > 32:  # 简单上限, 避免行业切换导致字典无限增长
        _CACHE.clear()
    _CACHE[key] = (time.time(), items)


async def calc_a_cash(industry: str = "", refresh: bool = False) -> list[dict]:
    """全市场 A 股现金市值比 (未过滤), 按比值降序。

    industry 非空时按行业子串过滤。每项含 ts_code / symbol / name / industry /
    cash_mv_ratio / money_cap / total_mv (万元) / close / pct_chg / pe_ttm / pb /
    roe / gross_margin / financial_year。
    """
    stocks = await ds._stock_basic()
    if industry.strip():
        stocks = stocks[stocks["industry"].str.contains(industry.strip(), na=False)]
    if stocks.empty:
        return []
    codes = [str(c) for c in stocks["ts_code"]]
    money_map = await pg.latest_money_cap_map(codes)  # money_cap 单位: 亿元
    if not money_map:
        return []

    pro = ds._init_pro()
    # 最新交易日收盘/涨跌幅 (一次批量)
    dmap: dict[str, dict] = {}
    try:
        _, display_td = await ds._display_trade_dates(pro)
        daily = await pro.daily(trade_date=display_td, fields="ts_code,close,pct_chg")
        if daily is not None and not daily.empty:
            for _, r in daily.iterrows():
                dmap[str(r["ts_code"])] = {
                    "close": ds._to_float(r.get("close")),
                    "pct_chg": ds._to_float(r.get("pct_chg")),
                }
    except Exception:
        pass
    # 最新估值 (一次批量, 含 total_mv / pe_ttm / pb)
    try:
        basic_map = await ds._latest_basic_map(pro, refresh=refresh)
    except Exception:
        basic_map = {}

    out: list[dict] = []
    for _, s in stocks.iterrows():
        code = str(s["ts_code"])
        m = money_map.get(code)
        if not m:
            continue
        b = basic_map.get(code) or {}
        total_mv = b.get("total_mv")                        # 万元
        money_wan = (m.get("money_cap") or 0.0) * 10000.0   # 亿元 → 万元
        if not total_mv or total_mv <= 0 or money_wan <= 0:
            continue
        d = dmap.get(code) or {}
        out.append({
            "ts_code": code,
            "symbol": str(s.get("symbol") or ""),
            "name": str(s.get("name") or ""),
            "industry": str(s.get("industry") or ""),
            "cash_mv_ratio": round(money_wan / float(total_mv) * 100.0, 2),
            "money_cap": round(money_wan, 2),
            "total_mv": round(float(total_mv), 2),
            "close": d.get("close"),
            "pct_chg": d.get("pct_chg"),
            "pe_ttm": b.get("pe_ttm"),
            "pb": b.get("pb"),
            "roe": m.get("roe"),
            "gross_margin": m.get("gross_margin"),
            "financial_year": m.get("year"),
        })
    out.sort(key=lambda x: x["cash_mv_ratio"], reverse=True)
    return out


async def _hk_latest_close_map(codes: list[str], refresh: bool = False) -> dict[str, float]:
    """腾讯批量实时行情 → {ts_code: 最新收盘} (失败返回空 dict, 由调用方兜底)。

    refresh=True 时忽略 5 分钟缓存, 强制重新拉取 (「重新同步数据」用)。
    """
    try:
        df = await (hkd.qt_quotes_force(codes) if refresh else hkd.qt_quotes_cached(codes))
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    out: dict[str, float] = {}
    for _, r in df.iterrows():
        sym = str(r.get("symbol") or "").replace("hk", "").strip()
        close = ds._to_float(r.get("close"))
        if not sym or close is None:
            continue
        try:
            out[f"{int(sym):05d}.HK"] = float(close)
        except (TypeError, ValueError):
            continue
    return out


async def calc_hk_cash(industry: str = "", refresh: bool = False) -> list[dict]:
    """全市场港股现金市值比 (未过滤), 按比值降序。

    总市值按「财年末市值 × 最新收盘 / 财年末收盘」折算到最新价; 无最新行情时退回财年末市值。
    """
    rows = await pg.hk_latest_cash_map(industry)
    if not rows:
        return []
    closes = await _hk_latest_close_map([str(r["ts_code"]) for r in rows], refresh=refresh)

    out: list[dict] = []
    for r in rows:
        code = str(r["ts_code"])
        money = float(r.get("money_cap") or 0.0)     # 万港元
        total_mv = float(r.get("total_mv") or 0.0)   # 万港元 (财年末)
        if money <= 0 or total_mv <= 0:
            continue
        last = closes.get(code)
        year_close = ds._to_float(r.get("close"))
        if last and year_close and year_close > 0:
            total_mv = total_mv * last / year_close  # 折算到最新市值
        out.append({
            "ts_code": code,
            "symbol": str(r.get("symbol") or ""),
            "name": str(r.get("name") or ""),
            "industry": str(r.get("industry") or ""),
            "market": str(r.get("market") or ""),
            "cash_mv_ratio": round(money / total_mv * 100.0, 2),
            "money_cap": round(money, 2),
            "total_mv": round(total_mv, 2),
            "close": last if last else year_close,
            "pct_chg": None,
            "roe": r.get("roe"),
            "gross_margin": r.get("gross_margin"),
            "financial_year": int(r.get("year") or 0) or None,
        })
    out.sort(key=lambda x: x["cash_mv_ratio"], reverse=True)
    return out


def _apply_filters(items: list[dict], filters: dict | None) -> list[dict]:
    """阈值筛选: {字段: {min, max}}, 支持 cash_mv_ratio(%) / total_mv(亿) / roe / gross_margin。

    缺数据 (None) 视为不满足阈值; total_mv 内部单位为万元, 与前端输入的「亿元」换算比较。
    """
    out: list[dict] = []
    for it in items:
        ok = True
        for key, flt in (filters or {}).items():
            if key not in _FILTER_KEYS:
                continue
            if isinstance(flt, dict):
                mn, mx = flt.get("min"), flt.get("max")
            else:
                mn, mx = flt, None
            v = it.get(key)
            if v is None:
                ok = False
                break
            if key == "total_mv":
                v = float(v) / 10000.0   # 万元 → 亿元
            if mn is not None and v < mn:
                ok = False
                break
            if mx is not None and v > mx:
                ok = False
                break
        if ok:
            out.append(it)
    return out


async def get_cash_screen(market: str = "a", industry: str = "",
                          filters: dict | None = None, limit: int = 500,
                          refresh: bool = False) -> list[dict]:
    """现金选股入口: market = 'a' | 'hk'; 返回按现金市值比降序的命中列表。

    refresh=True (「重新同步数据」) 时丢弃本服务缓存, 并强制刷新上游 15 分钟估值缓存 /
    5 分钟腾讯行情缓存, 重新拉取最新数据计算。
    """
    market = "hk" if market == "hk" else "a"
    key = f"{market}:{industry.strip()}"
    if refresh:
        _CACHE.pop(key, None)
    items = _cache_get(key)
    if items is None:
        items = await (calc_hk_cash(industry, refresh=refresh) if market == "hk"
                       else calc_a_cash(industry, refresh=refresh))
        _cache_put(key, items)
    return _apply_filters(items, filters)[:int(limit)]