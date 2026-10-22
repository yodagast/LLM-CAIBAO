"""申万一级行业指数 (31 个) 行情统计服务。

数据来源:
  - 申万宏源官网指数发布接口 (https://www.swsresearch.com/institute-sw/api/index_publish/history_home/)
    → 最新交易日快照: 开/高/低/收指数、成交量、成交额、涨跌幅、昨收
  - tushare index_classify(level=L1, src=SW2021)
    → 31 个申万一级行业代码与名称
  - tushare index_weekly
    → 近 52 周周线 high/low, 用于计算 52 周最高/最低与分位数

指标口径:
  涨跌幅(%)   = (最新收盘指数 − 昨收指数) / 昨收指数 × 100
  成交额(亿元) = 官网 bargainsum (亿元)
  成交量(亿股) = 官网 bargainamount (亿股)
  52周最高/最低 = 近 52 周周线 high 最大值 / low 最小值
  分位数       = (最新收盘 − 52周最低) / (52周最高 − 52周最低), 0~1 (越高越接近高位)

说明: tushare 的 sw_daily 接口当前 token 无权限, index_daily 对 .SI 返回空,
      故最新快照改用申万官网官方接口 (published by 申万宏源研究所)。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import httpx
import pandas as pd

from . import data_service

# 申万官网指数发布接口 (最新交易日快照, 免鉴权)
_SWS_BASE = "https://www.swsresearch.com/institute-sw/api/index_publish/"
_SWS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.swsresearch.com/",
}
# 官网证书链不完整 (缺少中间 CA), httpx 默认校验证书会失败
_SWS_VERIFY = False

# 官网分页并发度 (接口每页固定返回 10 条, 全市场约 445 条 → 45 页)
_SWS_CONCURRENCY = 16

# 全量快照缓存 (15 分钟)
_SNAPSHOT_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SNAPSHOT_TTL = 15 * 60

# 行业列表缓存 (1 小时)
_L1_CACHE: dict[str, tuple[float, list[dict]]] = {}
_L1_TTL = 60 * 60

# 52 周高低缓存 (30 分钟)
_HL_CACHE: dict[str, tuple[float, dict[str, tuple[float, float]]]] = {}
_HL_TTL = 30 * 60


def _num(v) -> float | None:
    """安全转 float (NaN/None/空串 → None)。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if not pd.isna(f) else None
    except (TypeError, ValueError):
        return None


async def sw_l1_industries() -> list[dict]:
    """申万一级行业列表 (31 个): [{index_code: 801010.SI, name: 农林牧渔}]。"""
    now = time.time()
    hit = _L1_CACHE.get("l1")
    if hit and now - hit[0] < _L1_TTL:
        return hit[1]
    pro = data_service._init_pro()
    rows: list[dict] = []
    try:
        df = await pro.index_classify(level="L1", src="SW2021")
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                code = str(r.get("index_code") or "").strip()
                name = str(r.get("industry_name") or "").strip()
                if code and name:
                    rows.append({"index_code": code, "name": name})
    except Exception:
        rows = []
    if not rows:   # 兜底: 硬编码 31 个申万一级行业 (SW2021)
        rows = [{"index_code": c, "name": n} for c, n in _SW_L1_FALLBACK]
    _L1_CACHE["l1"] = (now, rows)
    return rows


# 申万一级行业兜底表 (SW2021, tushare 不可用时使用)
_SW_L1_FALLBACK = [
    ("801010.SI", "农林牧渔"), ("801030.SI", "基础化工"), ("801040.SI", "钢铁"),
    ("801050.SI", "有色金属"), ("801080.SI", "电子"), ("801110.SI", "家用电器"),
    ("801120.SI", "食品饮料"), ("801130.SI", "纺织服饰"), ("801140.SI", "轻工制造"),
    ("801150.SI", "医药生物"), ("801160.SI", "公用事业"), ("801170.SI", "交通运输"),
    ("801180.SI", "房地产"), ("801200.SI", "商贸零售"), ("801210.SI", "社会服务"),
    ("801230.SI", "综合"), ("801710.SI", "建筑材料"), ("801720.SI", "建筑装饰"),
    ("801730.SI", "电力设备"), ("801740.SI", "国防军工"), ("801750.SI", "计算机"),
    ("801760.SI", "传媒"), ("801770.SI", "通信"), ("801780.SI", "银行"),
    ("801790.SI", "非银金融"), ("801880.SI", "汽车"), ("801890.SI", "机械设备"),
    ("801950.SI", "煤炭"), ("801960.SI", "石油石化"), ("801970.SI", "环保"),
    ("801980.SI", "美容护理"),
]


async def sw_snapshot() -> list[dict]:
    """申万全行业最新交易日快照 (官网 history_home 分页并发抓全, 带缓存)。

    官网接口每页固定返回 10 条 (pageSize 参数无效), 故先请求第 1 页拿到总数,
    再并发抓取剩余页 (串行 45 页约 90s, 并发后约 3s)。

    返回 [{code: 801010, name: 农林牧渔, date, open, high, low, close,
           prev_close, pct_chg, volume(亿股), amount(亿元)}]
    """
    now = time.time()
    hit = _SNAPSHOT_CACHE.get("all")
    if hit and now - hit[0] < _SNAPSHOT_TTL:
        return hit[1]

    out: list[dict] = []

    def _parse(rows: list[dict], seen: set[str], acc: list[dict]) -> None:
        for row in rows:
            code = str(row.get("swindexcode") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            close = _num(row.get("closeindex"))
            prev = _num(row.get("prevcloseindex"))
            pct = None
            if close is not None and prev:
                pct = (close - prev) / prev * 100.0
            acc.append({
                "code": code,
                "name": str(row.get("swindexname") or "").strip(),
                "date": str(row.get("bargaindate") or "")[:10].replace("-", ""),
                "open": _num(row.get("openindex")),
                "high": _num(row.get("maxindex")),
                "low": _num(row.get("minindex")),
                "close": close,
                "prev_close": prev,
                "pct_chg": pct,
                "volume": _num(row.get("bargainamount")),   # 亿股
                "amount": _num(row.get("bargainsum")),      # 亿元
            })

    try:
        async with httpx.AsyncClient(timeout=25.0, verify=_SWS_VERIFY,
                                     headers=_SWS_HEADERS) as client:
            first = await client.get(_SWS_BASE + "history_home/", params={"page": 1})
            if first.status_code == 200:
                data = first.json().get("data") or {}
                rows = data.get("results") or []
                total = int(data.get("count") or 0)
                seen: set[str] = set()
                _parse(rows, seen, out)
                # 每页 10 条 → 计算还需页数 (上限 80 页防异常)
                pages = min(80, max(1, -(-total // max(1, len(rows))))) if rows else 0
                if pages > 1:
                    sem = asyncio.Semaphore(_SWS_CONCURRENCY)

                    async def _page(p: int):
                        async with sem:
                            # 官网偶发超时/502, 重试 3 次避免丢失整页数据
                            for attempt in range(3):
                                try:
                                    r = await client.get(_SWS_BASE + "history_home/",
                                                         params={"page": p})
                                    if r.status_code == 200:
                                        return (r.json().get("data")
                                                or {}).get("results") or []
                                except Exception:
                                    pass
                                await asyncio.sleep(0.5 * (attempt + 1))
                            return []

                    for chunk in await asyncio.gather(
                            *[_page(p) for p in range(2, pages + 1)]):
                        _parse(chunk, seen, out)
    except Exception:
        out = []
    if out:
        _SNAPSHOT_CACHE["all"] = (now, out)
    return out


async def _sw_52w_high_low(codes: list[str]) -> dict[str, tuple[float, float]]:
    """近 52 周周线 high 最大值 / low 最小值 (tushare index_weekly, 带缓存)。"""
    now = time.time()
    hit = _HL_CACHE.get("all")
    if hit and now - hit[0] < _HL_TTL:
        cached = hit[1]
        if all(c in cached for c in codes):
            return cached

    pro = data_service._init_pro()
    start = (datetime.now() - timedelta(days=420)).strftime("%Y%m%d")
    result: dict[str, tuple[float, float]] = dict(hit[1]) if hit else {}

    async def one(code: str) -> None:
        ts_code = code if code.endswith(".SI") else f"{code}.SI"
        try:
            df = await pro.index_weekly(ts_code=ts_code, start_date=start, end_date="")
        except Exception:
            return
        if df is None or df.empty:
            return
        d = df.sort_values("trade_date").tail(52)   # 最近 52 周
        hi, lo = _num(d["high"].max()), _num(d["low"].min())
        if hi is not None and lo is not None:
            result[code] = (hi, lo)

    await asyncio.gather(*[one(c) for c in codes])
    _HL_CACHE["all"] = (now, result)
    return result


async def screen_sw_indexes(sort_by: str = "pct_chg", order: str = "desc") -> dict:
    """统计申万 31 个一级行业指数: 涨跌/成交额/成交量/52周高低/分位数。"""
    industries = await sw_l1_industries()
    snapshot = await sw_snapshot()
    snap_map = {s["code"]: s for s in snapshot}

    codes = [ind["index_code"] for ind in industries]
    hl = await _sw_52w_high_low(codes)

    items: list[dict] = []
    for ind in industries:
        code = ind["index_code"]
        plain = code.split(".")[0]
        s = snap_map.get(plain) or {}
        close = s.get("close")
        high52, low52 = hl.get(code, (None, None))
        # tushare 周线最后一根通常是上一周 (滞后于官网当日快照), 把当日 高/低/收 纳入区间,
        # 否则会出现 close < 52周最低 → 分位数为负的异常值
        cand_hi = [v for v in (high52, close, s.get("high")) if v is not None]
        cand_lo = [v for v in (low52, close, s.get("low")) if v is not None]
        if cand_hi:
            high52 = max(cand_hi)
        if cand_lo:
            low52 = min(cand_lo)
        pos52 = None
        if close is not None and high52 is not None and low52 is not None and high52 > low52:
            pos52 = (close - low52) / (high52 - low52)
        items.append({
            "index_code": code,
            "code": plain,
            "name": ind["name"],
            "date": s.get("date") or "",
            "close": close,
            "pct_chg": s.get("pct_chg"),
            "open": s.get("open"),
            "high": s.get("high"),
            "low": s.get("low"),
            "prev_close": s.get("prev_close"),
            "amount": s.get("amount"),      # 成交额 (亿元)
            "volume": s.get("volume"),      # 成交量 (亿股)
            "high52": high52,
            "low52": low52,
            "pos52": pos52,
            # 距 52 周高点/低点的偏离度 (%), 便于判断位置
            "dev_high_pct": round((close - high52) / high52 * 100.0, 2)
                            if (close is not None and high52) else None,
            "dev_low_pct": round((close - low52) / low52 * 100.0, 2)
                           if (close is not None and low52) else None,
        })

    # 排序 (NULL 值排最后)
    key = sort_by if sort_by in {
        "index_code", "code", "name", "close", "pct_chg", "amount", "volume",
        "high52", "low52", "pos52", "dev_high_pct", "dev_low_pct",
    } else "pct_chg"
    reverse = str(order).lower() != "asc"

    def _k(it: dict):
        v = it.get(key)
        if v is None:
            return (1, 0.0) if reverse else (1, 0.0)
        if isinstance(v, str):
            return (0, v)
        return (0, float(v))

    if key in ("index_code", "code", "name"):
        items.sort(key=lambda it: str(it.get(key) or ""), reverse=reverse)
    else:
        items.sort(key=_k, reverse=reverse)

    valid = [it for it in items if it.get("close") is not None]
    calc_date = ""
    if valid:
        calc_date = max((it.get("date") or "") for it in valid)
    return {
        "count": len(items),
        "items": items,
        "calc_date": calc_date,
        "sort_by": key,
        "order": "asc" if not reverse else "desc",
        "source": "swsresearch+tushare",
        "snapshot_rows": len(snapshot),
    }


async def sync_sw_indexes_to_db() -> dict:
    """计算申万 31 个一级行业指数统计并写入 PostgreSQL (定时任务入口)。"""
    from . import pg_service
    await pg_service.init_sw_index_schema()
    data = await screen_sw_indexes()
    rows = []
    for it in data["items"]:
        r = dict(it)
        r["calc_date"] = data.get("calc_date") or ""
        rows.append(r)
    stored = await pg_service.upsert_sw_index_rows(rows)
    return {"total": len(rows), "stored": stored,
            "calc_date": data.get("calc_date") or "",
            "snapshot_rows": data.get("snapshot_rows")}


async def get_sw_indexes(sort_by: str = "pct_chg", order: str = "desc") -> dict:
    """申万一级行业指数统计 (读库优先, 无数据时实时计算并入库)。"""
    from . import pg_service
    try:
        calc_date = await pg_service.latest_sw_index_calc_date()
    except Exception:
        calc_date = ""
    if calc_date:
        items = await pg_service.query_sw_index(calc_date, sort_by, order)
        if items:
            return {"count": len(items), "items": items, "calc_date": calc_date,
                    "sort_by": sort_by, "order": order, "source": "db"}
    data = await screen_sw_indexes(sort_by, order)
    try:
        rows = [dict(it, calc_date=data.get("calc_date") or "") for it in data["items"]]
        await pg_service.upsert_sw_index_rows(rows)
        data["source"] = "live"
    except Exception:
        pass
    return data