"""tushare 数据服务层 (异步 aio)。

职责:
  - 从 ../.env 读取 TUSHARE_TOKEN 并初始化 tushare 异步客户端 (httpx)
  - 股票代码解析: 6 位代码 → ts_code + 名称 (支持股票 / ETF)
  - 日线数据获取 (股票 daily / ETF fund_daily), 带内存缓存
  - 股票关键字搜索 (前端联想)

约定 (与 strategy/ 下脚本一致):
  - token 从项目根目录 .env 读取 (TUSHARE_TOKEN)
  - 所有 tushare 访问均为 aio (httpx AsyncClient), 调用点需 await
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

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


class _AsyncPro:
    """tushare 异步客户端 (httpx AsyncClient), 方法名与官方 tushare pro 一致, 返回 DataFrame。

    调用示例: `await pro.daily(ts_code="600036.SH", start_date="20240101", end_date="20240201")`。
    """

    BASE_URL = "http://api.tushare.pro"

    def __init__(self, token: str) -> None:
        self._token = token
        self._client: httpx.AsyncClient | None = None

    async def _client_ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=30.0)
        return self._client

    @staticmethod
    def _nonempty(params: dict) -> dict:
        return {k: v for k, v in params.items() if v is not None and v != ""}

    # tushare 限频/额度错误码 (token 每分钟调用次数限制等)
    _FREQ_LIMIT_CODES = {40001, 40002, 40003, 40004, 40005, 40006, 40007, 40008, 40009, 40010}

    async def _call(self, api_name: str, params: dict | None = None, fields: str = "") -> pd.DataFrame:
        client = await self._client_ensure()
        body = {"api_name": api_name, "token": self._token,
                "params": params or {}, "fields": fields}
        for attempt in range(4):
            resp = await client.post("", json=body)
            data = resp.json()
            if data.get("code", -1) == 0:
                flds = data["data"]["fields"]
                items = data["data"]["items"] or []
                return pd.DataFrame(items, columns=flds)
            code = data.get("code")
            msg = str(data.get("msg") or "")
            # 限频/额度不足: 退避重试 (避免低频 token 连续调用被拒, 导致快照等批量场景失败)
            if code in self._FREQ_LIMIT_CODES or "频率" in msg or "次数" in msg or "权限" in msg:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            raise RuntimeError(f"tushare {api_name} 失败: {msg or data}")
        raise RuntimeError(f"tushare {api_name} 限频重试仍失败: {data.get('msg') or data}")

    async def daily(self, **kw) -> pd.DataFrame:
        return await self._call("daily", self._nonempty(kw))

    async def daily_basic(self, **kw) -> pd.DataFrame:
        return await self._call("daily_basic", self._nonempty(kw))

    async def weekly(self, **kw) -> pd.DataFrame:
        return await self._call("weekly", self._nonempty(kw))

    async def monthly(self, **kw) -> pd.DataFrame:
        return await self._call("monthly", self._nonempty(kw))

    async def dividend(self, **kw) -> pd.DataFrame:
        return await self._call("dividend", self._nonempty(kw))

    async def stock_basic(self, **kw) -> pd.DataFrame:
        return await self._call("stock_basic", self._nonempty(kw))

    async def fund_basic(self, **kw) -> pd.DataFrame:
        return await self._call("fund_basic", self._nonempty(kw))

    async def fund_daily(self, **kw) -> pd.DataFrame:
        return await self._call("fund_daily", self._nonempty(kw))

    async def fund_share(self, **kw) -> pd.DataFrame:
        return await self._call("fund_share", self._nonempty(kw))

    async def fund_nav(self, **kw) -> pd.DataFrame:
        return await self._call("fund_nav", self._nonempty(kw))

    async def fina_indicator(self, **kw) -> pd.DataFrame:
        return await self._call("fina_indicator", self._nonempty(kw))

    async def balancesheet(self, **kw) -> pd.DataFrame:
        return await self._call("balancesheet", self._nonempty(kw))

    async def income(self, **kw) -> pd.DataFrame:
        return await self._call("income", self._nonempty(kw))

    async def cashflow(self, **kw) -> pd.DataFrame:
        return await self._call("cashflow", self._nonempty(kw))

    async def adj_factor(self, **kw) -> pd.DataFrame:
        return await self._call("adj_factor", self._nonempty(kw))

    async def hk_basic(self, **kw) -> pd.DataFrame:
        return await self._call("hk_basic", self._nonempty(kw))

    async def pro_bar(self, ts_code: str = "", freq: str = "D", adj: str | None = None,
                      start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """周/月/日 K 线, 支持 qfq/hfq 复权 (与官方 ts.pro_bar 逻辑等价)。"""
        if freq == "W":
            df = await self.weekly(ts_code=ts_code, start_date=start_date, end_date=end_date)
        elif freq == "M":
            df = await self.monthly(ts_code=ts_code, start_date=start_date, end_date=end_date)
        else:
            df = await self.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return df
        df = df.sort_values("trade_date").reset_index(drop=True)
        if adj in ("qfq", "hfq"):
            af = await self.adj_factor(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if af is not None and not af.empty:
                af = af.sort_values("trade_date").reset_index(drop=True)
                df = df.merge(af[["trade_date", "adj_factor"]], on="trade_date", how="left")
                df["adj_factor"] = df["adj_factor"].ffill()
                last_af = df["adj_factor"].iloc[-1]
                if last_af and last_af > 0:
                    factor = df["adj_factor"] / last_af if adj == "qfq" else df["adj_factor"]
                    for c in ("open", "high", "low", "close"):
                        df[c] = df[c] * factor
                    df["pre_close"] = df["close"].shift(1).fillna(df["close"])
                df = df.drop(columns=["adj_factor"])
        return df


def _init_pro() -> _AsyncPro:
    _load_env_token()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置 TUSHARE_TOKEN, 请在项目根目录 .env 中配置。")
    return _AsyncPro(token)


# ---------------------------------------------------------------------------
# 股票基本信息 / 代码解析
# ---------------------------------------------------------------------------

async def _stock_basic() -> pd.DataFrame:
    """获取全部上市股票基本信息 (TTL 缓存 1h, 异步)。"""
    hit = _cache_get(_STOCK_BASIC_CACHE, "all", _TTL_STOCK_BASIC)
    if hit is not None:
        return hit
    async with _BASIC_LOCK:
        hit = _cache_get(_STOCK_BASIC_CACHE, "all", _TTL_STOCK_BASIC)
        if hit is not None:
            return hit
        pro = _init_pro()
        df = await pro.stock_basic(exchange="", list_status="L",
                                   fields="ts_code,symbol,name,industry,market,list_date")
        _cache_put(_STOCK_BASIC_CACHE, "all", df)
        return df


async def _fund_basic() -> pd.DataFrame:
    """获取全部上市基金基本信息 (TTL 缓存 1h, 异步)。"""
    hit = _cache_get(_FUND_BASIC_CACHE, "all", _TTL_FUND_BASIC)
    if hit is not None:
        return hit
    async with _BASIC_LOCK:
        hit = _cache_get(_FUND_BASIC_CACHE, "all", _TTL_FUND_BASIC)
        if hit is not None:
            return hit
        pro = _init_pro()
        try:
            df = await pro.fund_basic(market="E")
        except Exception:
            df = pd.DataFrame(columns=["ts_code", "name"])
        # fund_basic 无 symbol 列, 由 ts_code 推导 (如 513050.SH → 513050)
        if "symbol" not in df.columns:
            df["symbol"] = df["ts_code"].str.split(".").str[0]
        _cache_put(_FUND_BASIC_CACHE, "all", df)
        return df


async def _resolve_hk(ts_code: str) -> dict:
    """解析港股代码 → {"ts_code", "symbol", "name", "kind", "market"}。

    延迟导入 hk_data_service 避免循环依赖 (hk_data_service 依赖本模块的 _init_pro)。
    """
    from . import hk_data_service
    stocks = await hk_data_service.hk_stock_list()
    hit = stocks[stocks["ts_code"].astype(str) == ts_code]
    if hit.empty:
        raise ValueError(f"未找到港股代码 [{ts_code}]。")
    row = hit.iloc[0]
    return {
        "ts_code": ts_code,
        "symbol": ts_code.split(".")[0],
        "name": str(row.get("name") or ""),
        "kind": "hk",
        "market": "HK",
    }


async def resolve_code(code: str) -> dict:
    """解析股票/ETF/港股 代码, 返回 {"ts_code", "symbol", "name", "kind"}。

    支持:
      - A股/基金: 6 位数字代码 (如 "000858" → "000858.SZ") 或带后缀 ("513050.SH")
      - 港股: 带 .HK 后缀 ("00700.HK") 或 4~5 位数字代码 (如 "0700"/"700" → "00700.HK")
      - kind: stock(A股) / fund(基金ETF) / hk(港股)
    """
    code = code.strip().upper()
    if not code:
        raise ValueError("股票代码不能为空。")

    # 规范输入: 去掉空格等
    ts_code = code if "." in code else None

    # 港股: 显式 .HK 后缀
    if ts_code is not None and ts_code.endswith(".HK"):
        return await _resolve_hk(ts_code)
    # 港股: 4~5 位纯数字代码 (A股均为 6 位, 5 位即视为港股零填充)
    if ts_code is None and code.isdigit() and 4 <= len(code) <= 5:
        return await _resolve_hk(f"{code.zfill(5)}.HK")

    stocks = await _stock_basic()
    funds = await _fund_basic()

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
    # 港股带 .HK 后缀但不在 A股表 (如 00700.HK 前缀等)
    if ts_code.endswith(".HK"):
        return await _resolve_hk(ts_code)
    raise ValueError(f"未找到代码 [{ts_code}] 对应的股票/基金。")


# A股股票代码前缀: 6(SH) 0/3(SZ) 4/8(北交所); 其余 1/5 开头多为基金/ETF
FUND_PREFIXES = ("51", "56", "58", "50", "15", "16", "18", "159", "160", "161", "162", "163", "164", "165", "166", "167", "168", "169", "180", "181", "182", "183", "184", "185", "186", "187", "188", "189")


def _is_fund_code(ts_code: str) -> bool:
    """判断 ts_code (如 513050.SH) 是否为基金/ETF 代码。"""
    symbol = ts_code.split(".")[0]
    return symbol.startswith(FUND_PREFIXES)


async def search_stock(keyword: str, limit: int = 20) -> list[dict]:
    """按代码或名称模糊搜索股票/基金, 供前端联想使用。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    stocks = await _stock_basic()
    mask = (
        stocks["symbol"].str.contains(keyword, na=False)
        | stocks["ts_code"].str.contains(keyword, na=False)
        | stocks["name"].str.contains(keyword, na=False)
    )
    hits = stocks[mask].head(limit)
    items = [
        {"ts_code": row["ts_code"], "symbol": row["symbol"], "name": row["name"], "kind": "stock"}
        for _, row in hits.iterrows()
    ]

    # 若股票结果不足, 补充基金/ETF (常见于输入 5/1 开头代码或基金名称)
    if len(items) < limit:
        funds = await _fund_basic()
        fmask = (
            funds["symbol"].str.contains(keyword, na=False)
            | funds["ts_code"].str.contains(keyword, na=False)
            | funds["name"].str.contains(keyword, na=False)
        )
        fund_items = [
            {"ts_code": row["ts_code"], "symbol": row["symbol"], "name": row["name"], "kind": "fund"}
            for _, row in funds[fmask].head(limit - len(items)).iterrows()
        ]
        items.extend(fund_items)

    # 补充港股 (输入 4~5 位代码或 .HK 后缀或港股名称时)
    if len(items) < limit:
        try:
            from . import hk_data_service
            hk = await hk_data_service.hk_stock_list()
            hkmask = (
                hk["ts_code"].astype(str).str.contains(keyword, na=False)
                | hk["name"].astype(str).str.contains(keyword, na=False)
            )
            hk_items = [
                {"ts_code": str(row["ts_code"]), "symbol": str(row["ts_code"]).split(".")[0],
                 "name": str(row.get("name") or ""), "kind": "hk", "market": "HK"}
                for _, row in hk[hkmask].head(limit - len(items)).iterrows()
            ]
            items.extend(hk_items)
        except Exception:
            pass
    return items[:limit]


async def search_industries(keyword: str = "", limit: int = 20) -> list[dict]:
    """行业模糊搜索: 返回匹配的行业及所含股票数量 (供前端候选推荐)。

    按关键词子串匹配行业名, 无关键词时返回股票数最多的热门行业;
    结果按股票数降序。
    """
    stocks = await _stock_basic()
    counts = stocks["industry"].value_counts()
    keyword = (keyword or "").strip()
    if keyword:
        mask = counts.index.str.contains(keyword, na=False)
        counts = counts[mask]
    items = [
        {"industry": str(ind), "count": int(c)}
        for ind, c in counts.head(limit).items()
    ]
    return items


# ---------------------------------------------------------------------------
# 日线数据 (带缓存)
# ---------------------------------------------------------------------------

_DAILY_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_TTL_DAILY = 1800  # 日线 30 分钟 (低频变化; 无 TTL 会永久缓存占内存且数据陈旧)

# 详情页缓存 (避免每次重复拉取): daily_basic / dividend / stock_basic
_DAILY_BASIC_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_DIVIDEND_CACHE: dict[str, tuple[float, object]] = {}
_STOCK_BASIC_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_FUND_BASIC_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_TTL_DAILY_BASIC = 900     # 15 分钟 (估值/换手率每日更新)
_TTL_DIVIDEND = 3600       # 1 小时 (分红低频变化)
_TTL_STOCK_BASIC = 3600    # 1 小时 (全市场列表)
_TTL_FUND_BASIC = 3600     # 1 小时 (全市场基金列表)

# 防并发缓存穿透: 批量场景 (如自选股列表) 多个协程同时 miss 时, 只放行一个去拉取
_BASIC_LOCK = asyncio.Lock()


def _cache_get(cache: dict, key: str, ttl: float) -> pd.DataFrame | None:
    hit = cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _cache_put(cache: dict, key: str, value) -> None:
    cache[key] = (time.time(), value)


async def _pg_daily_basic_df(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """从本地 stock_daily_basic 读取估值/换手率; 覆盖足够时返回 DataFrame, 否则 None。

    返回 df 列: trade_date(YYYYMMDD) / close / pb / pe / pe_ttm / total_share /
    float_share / total_mv / circ_mv / dv_ratio / dv_ttm / turnover_rate。
    """
    from . import pg_service
    symbol = str(ts_code)  # 完整 ts_code (带后缀), 与 stock_daily_bars 一致
    try:
        stats = await pg_service.daily_basic_stats(symbol)
    except Exception:
        return None
    if not stats or stats["n"] < 30:
        return None
    s = (start_date or "20000101")[:8]
    e = (end_date or datetime.now().strftime("%Y%m%d"))[:8]
    mn = stats["min_date"].replace("-", "")[:8]
    mx = stats["max_date"].replace("-", "")[:8]
    if mn > _add_days(s, 400):
        return None
    if mx < _add_days(e, -45):
        return None
    try:
        rows = await pg_service.query_daily_basic(symbol, s, e)
    except Exception:
        return None
    if not rows or len(rows) < 20:
        return None
    df = pd.DataFrame(rows)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    for c in ("close", "pb", "pe", "pe_ttm", "total_share", "float_share",
              "total_mv", "circ_mv", "dv_ratio", "dv_ttm", "turnover_rate"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.attrs["data_source"] = "pg"
    return df


async def _backfill_daily_basic_one(ts_code: str, df: pd.DataFrame) -> None:
    """把 tushare daily_basic 回填到本地 stock_daily_basic (幂等 upsert)。"""
    from . import pg_service
    symbol = str(ts_code)  # 完整 ts_code (带后缀)
    rows = []
    for _, r in df.iterrows():
        td = str(r["trade_date"])
        try:
            d = datetime.strptime(td[:8], "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        rows.append((symbol, d, _to_float(r.get("close")), _to_float(r.get("pb")),
                     _to_float(r.get("pe")), _to_float(r.get("pe_ttm")),
                     _to_float(r.get("total_share")), _to_float(r.get("float_share")),
                     _to_float(r.get("total_mv")), _to_float(r.get("circ_mv")),
                     _to_float(r.get("dv_ratio")), _to_float(r.get("dv_ttm")),
                     _to_float(r.get("turnover_rate"))))
    if rows:
        await pg_service.upsert_daily_basic_rows(rows)


async def _get_daily_basic(pro, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """daily_basic 全历史窗口: 优先本地 pgsql (stock_daily_basic), 缺失时 tushare 并回填。

    带 15 分钟内存缓存。本地已有估值/换手率时不再打 tushare (详情页提速关键)。
    """
    hit = _cache_get(_DAILY_BASIC_CACHE, ts_code, _TTL_DAILY_BASIC)
    if hit is not None:
        return hit
    try:
        pg_df = await _pg_daily_basic_df(ts_code, start_date, end_date)
        if pg_df is not None and not pg_df.empty:
            _cache_put(_DAILY_BASIC_CACHE, ts_code, pg_df)
            return pg_df
    except Exception:
        pass
    b = await pro.daily_basic(
        ts_code=ts_code, start_date=start_date, end_date=end_date,
        fields="trade_date,close,pb,pe,pe_ttm,total_share,float_share,total_mv,circ_mv,dv_ratio,dv_ttm,turnover_rate")
    if b is not None and not b.empty:
        # 回填本地 pg (后台异步, 不阻塞本次响应)
        try:
            asyncio.create_task(_backfill_daily_basic_one(ts_code, b))
        except Exception:
            pass
    _cache_put(_DAILY_BASIC_CACHE, ts_code, b)
    return b


def _adj_close(df: pd.DataFrame) -> pd.Series:
    """用 pct_chg 重建前复权连续收盘价, 消除除权除息/份额拆分/分红跳空。

    tushare 的 daily/fund_daily 返回不复权价格, 遇拆分/分红时 close 会跳空
    (如 512170 2021-02-25 份额拆分 close 2.564→0.818), 直接算净值/收益会产生
    假暴跌。而 pct_chg 字段是按拆分/分红调整后的正确涨跌幅 (拆分日 pre_close 已
    同步调整), 从最新价向前累计即可得到连续的前复权价 (最新价=真实价)。
    """
    close = df["close"].astype(float)
    pct = df["pct_chg"].astype(float)
    n = len(df)
    out = close.to_numpy().copy()
    for i in range(n - 2, -1, -1):
        p = pct.iloc[i + 1]
        if pd.notna(p):
            out[i] = out[i + 1] / (1 + p / 100.0)
        elif close.iloc[i + 1] > 0:
            out[i] = out[i + 1] * (close.iloc[i] / close.iloc[i + 1])
        else:
            out[i] = out[i + 1]
    return pd.Series(out, index=df.index)


async def _get_hk_daily(ts_code: str, start_date: str, end_date: str,
                        adj: str = "") -> pd.DataFrame:
    """港股日线 (数据源: 腾讯港股 K 线, 不复权), 返回 tushare daily 同构 DataFrame。

    列: trade_date(YYYYMMDD) / open / high / low / close / vol / amount /
        pre_close / pct_chg。amount 为近似值 (成交量×收盘价)。
    注意: 港股不提供可靠前复权序列, adj="qfq" 时仍返回原始价 (与 A 股 qfq 口径不同)。
    """
    from . import hk_data_service
    df = await hk_data_service._tencent_kline_df(ts_code)
    if df.empty:
        raise ValueError(f"未获取到 {ts_code} 的港股日线数据。")
    # 日期过滤 (start_date/end_date 为 YYYYMMDD)
    s = start_date or "20000101"
    e = end_date or datetime.now().strftime("%Y%m%d")
    df = df[(df["date"] >= pd.to_datetime(s)) & (df["date"] <= pd.to_datetime(e))]
    if df.empty:
        raise ValueError(f"未获取到 {ts_code} 在 {s}~{e} 的港股日线数据。")
    df = df.reset_index(drop=True)
    close = df["close"].astype(float)
    out = pd.DataFrame({
        "trade_date": df["date"].dt.strftime("%Y%m%d"),
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": close,
        "vol": df["vol"].astype(float),
        # 成交额统一为千元 (与 tushare daily 口径一致); 港股 vol 单位为股, 成交额≈量×价
        "amount": (df["vol"] * df["close"] / 1000.0).astype(float),
        "pre_close": close.shift(1).fillna(close),
        "pct_chg": close.pct_change().fillna(0.0) * 100.0,
    })
    return out


def _add_days(ymd: str, days: int) -> str:
    """YYYYMMDD 加/减 N 天 (用于覆盖性判断)。"""
    try:
        return (datetime.strptime(ymd[:8], "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return ymd


async def _pg_daily_df(symbol: str, start_date: str, end_date: str, adj_key: str) -> pd.DataFrame | None:
    """从本地 pgsql stock_daily_bars 读取日线; 覆盖足够时返回 DataFrame (含 turnover_rate), 否则 None。

    这是「前端优先从 pgsql 加载」的核心: 本地已有数据时不再打 tushare。
    symbol 为完整 ts_code (如 600036.SH), 与 alpha158 写入约定一致。
    adj_key: "" 原始价 / "qfq" 前复权 / "hfq" 后复权 (用 adj_factor 重建)。
    返回 df 带 attrs["data_source"]="pg", 供接口透出数据源。
    """
    from . import pg_service
    try:
        stats = await pg_service.daily_bars_stats(symbol)
    except Exception:
        return None
    if not stats or stats["n"] < 60:
        return None
    s = (start_date or "20000101")[:8]
    e = (end_date or datetime.now().strftime("%Y%m%d"))[:8]
    # 覆盖性: 本地最早日不晚于请求起始+400天, 最晚日不早于请求结束-45天
    # (stats 中 min/max 为 date 类型, 转 str 形如 'YYYY-MM-DD', 先去横线)
    mn = stats["min_date"].replace("-", "")[:8]
    mx = stats["max_date"].replace("-", "")[:8]
    if mn > _add_days(s, 400):
        return None
    if mx < _add_days(e, -45):
        return None
    try:
        rows = await pg_service.query_daily_bars(symbol, s, e)
    except Exception:
        return None
    if not rows or len(rows) < 30:
        return None
    df = pd.DataFrame(rows)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
    for c in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount",
              "adj_factor", "turnover_rate"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("trade_date").reset_index(drop=True)
    if adj_key in ("qfq", "hfq"):
        has_af = "adj_factor" in df.columns and df["adj_factor"].notna().any()
        if has_af:
            df["adj_factor"] = df["adj_factor"].ffill()
            last_af = df["adj_factor"].iloc[-1]
            factor = (df["adj_factor"] / last_af) if adj_key == "qfq" else df["adj_factor"]
            for c in ("open", "high", "low", "close"):
                df[c] = df[c] * factor
            df["pre_close"] = df["close"].shift(1).fillna(df["close"])
        elif adj_key == "qfq":
            df["close"] = _adj_close(df)
            df["pre_close"] = df["close"].shift(1).fillna(df["close"])
    df.attrs["data_source"] = "pg"
    return df


async def get_daily(ts_code: str, kind: str = "stock",
                    start_date: str = "20170101", end_date: str = "",
                    adj: str = "") -> pd.DataFrame:
    """获取单只股票/ETF/港股 日线 (trade_date 升序), 带内存缓存。

    优先从本地 pgsql (stock_daily_bars) 加载 (前端优先从库读取); 本地无数据时回退 tushare。
    adj: "" 不复权(原始价格) / "qfq" 前复权 / "hfq" 后复权 (A股用复权因子或 pct_chg 重建;
    港股无可靠前复权, 返回原始价)。
    回测/收益计算建议用 "qfq" (A股); 港股回测用原始价 (数据源限制)。
    """
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    adj_key = str(adj).strip().lower()
    cache_key = f"{ts_code}:{kind}:{start_date}:{end_date}:{adj_key}"
    hit = _DAILY_CACHE.get(cache_key)
    if hit and (time.time() - hit[0]) < _TTL_DAILY:
        return hit[1]

    # 港股走腾讯日线
    if kind == "hk" or str(ts_code).endswith(".HK"):
        df = await _get_hk_daily(ts_code, start_date, end_date, adj_key)
        df = df.sort_values("trade_date").reset_index(drop=True)
        _DAILY_CACHE[cache_key] = (time.time(), df)
        return df

    # 优先本地 pgsql (前端优先从库加载)
    try:
        pg_df = await _pg_daily_df(ts_code, start_date, end_date, adj_key)
        if pg_df is not None and not pg_df.empty:
            _DAILY_CACHE[cache_key] = (time.time(), pg_df)
            return pg_df
    except Exception:
        pass

    pro = _init_pro()
    df = pd.DataFrame()
    if kind == "fund" or _is_fund_code(ts_code):
        try:
            df = await pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception:
            df = pd.DataFrame()
        if df.empty:
            df = await pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    else:
        df = await pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df.empty:
            df = await pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

    if df is None or df.empty:
        raise ValueError(f"未获取到 {ts_code} 在 {start_date}~{end_date} 的日线数据。")

    df = df.sort_values("trade_date").reset_index(drop=True)
    if adj_key == "qfq":
        df = df.copy()
        df["close"] = _adj_close(df)
        df["pre_close"] = df["close"].shift(1).fillna(df["close"])
    df.attrs["data_source"] = "tushare"
    _DAILY_CACHE[cache_key] = (time.time(), df)
    return df


async def get_quote(ts_code: str, kind: str = "stock", days: int = 120) -> pd.DataFrame:
    """获取最近 N 个交易日的行情数据 (升序), 供前端绘制行情曲线。"""
    end_date = datetime.now().strftime("%Y%m%d")
    # 按约 2.5 倍自然日预取, 以覆盖 N 个交易日
    start = (datetime.now() - timedelta(days=int(days * 2.5) + 30)).strftime("%Y%m%d")
    df = await get_daily(ts_code, kind, start_date=start, end_date=end_date)
    return df.tail(days).reset_index(drop=True)


def _df_to_bars(df: pd.DataFrame) -> list[dict]:
    """把日线 df (trade_date/OHLC/pre_close/pct_chg/vol/amount/turnover_rate) 转成 K线 bars。"""
    bars = []
    for _, row in df.iterrows():
        td = str(row["trade_date"])
        pre = _to_float(row.get("pre_close"))
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if (pre and pre > 0) else None
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": float(row["close"]),
            "pre_close": pre,
            "change": _to_float(row.get("change")),
            "pct_chg": float(row.get("pct_chg", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": _to_float(row.get("turnover_rate")),
        })
    return bars


def _df_to_agg_bars(df: pd.DataFrame, freq: str) -> list[dict]:
    """把日线 df 聚合成 周线(W)/月线(M) bars (同 _hk_kline 聚合口径, 支持复权后价格)。"""
    d = df.copy()
    d["_dt"] = pd.to_datetime(d["trade_date"], format="%Y%m%d")
    rule = "W" if freq.upper() == "W" else "ME"
    g = d.set_index("_dt").resample(rule)
    agg = pd.DataFrame({
        "trade_date": g["trade_date"].last().values,
        "open": g["open"].first().values,
        "high": g["high"].max().values,
        "low": g["low"].min().values,
        "close": g["close"].last().values,
        "vol": g["vol"].sum().values,
        "amount": g["amount"].sum().values,
        "pre_close": g["pre_close"].last().values,
    }).dropna(subset=["close"])
    bars = []
    prev = None
    for _, row in agg.iterrows():
        td = str(row["trade_date"])
        close = float(row["close"])
        pre = prev if prev is not None else close
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if pre and pre > 0 else None
        pct = (close / pre - 1) * 100.0 if pre and pre > 0 else 0.0
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": close,
            "pre_close": pre,
            "change": close - pre,
            "pct_chg": round(pct, 4),
            "vol": float(row.get("vol") or 0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": None,
        })
        prev = close
    return bars


async def get_kline(ts_code: str, kind: str = "stock", freq: str = "D", adj: str = "",
                    start_date: str = "", end_date: str = "", hist_years: int = 10) -> list[dict]:
    """获取 K 线 bars (周期: D日/W周/M月; 复权: qfq前复权/hfq后复权/空不复权)。

    基于 tushare pro_bar 接口 (异步)。返回升序 bars (含 open/high/low/close/pre_close/change/pct_chg/vol/amount/amplitude)。
    D 线额外附带 turnover_rate (换手率, 来自 daily_basic); W/M 线换手率为空。
    港股 (kind=hk / .HK) 走腾讯日线 (不复权), W/M 由日线聚合。
    """
    if kind == "hk" or str(ts_code).endswith(".HK"):
        return await _hk_kline(ts_code, freq, adj, start_date, end_date, hist_years)

    end = end_date or datetime.now().strftime("%Y%m%d")
    start = start_date or (datetime.now() - timedelta(days=int(hist_years * 365.25) + 10)).strftime("%Y%m%d")

    # 优先本地 pgsql: D/W/M 全部基于本地日线构建 (W/M 由日线 resample 聚合, 不再打 tushare)
    try:
        pg_df = await _pg_daily_df(ts_code, start, end, (adj or "").strip().lower())
        if pg_df is not None and not pg_df.empty:
            if freq.upper() == "D":
                return _df_to_bars(pg_df)
            return _df_to_agg_bars(pg_df, freq.upper())
    except Exception:
        pass

    adj_param = adj.strip() or None
    pro = _init_pro()
    # 并发: pro_bar 与 daily_basic(换手率) 相互独立 (切换周期/复权时避免串行叠加延迟)
    bar_task = asyncio.create_task(pro.pro_bar(ts_code=ts_code, freq=freq.upper(),
                                               adj=adj_param, start_date=start, end_date=end))
    basic_task = None
    if freq.upper() == "D" and kind != "fund":
        basic_task = asyncio.create_task(
            pro.daily_basic(ts_code=ts_code, start_date=start, end_date=end,
                            fields="trade_date,turnover_rate"))
    try:
        df = await bar_task
    except Exception as e:
        raise ValueError(f"获取 {ts_code} {freq}线{adj or '不复权'}数据失败: {e}")
    if df is None or df.empty:
        raise ValueError(f"未获取到 {ts_code} {freq}线 {adj or '不复权'} 数据")
    df = df.sort_values("trade_date").reset_index(drop=True)

    # 日线附带回换手率 (换手率不随复权改变, 仅日线粒度有)
    turnover_map = {}
    if basic_task is not None:
        try:
            tb = await basic_task
            if tb is not None and not tb.empty:
                turnover_map = {
                    str(r["trade_date"]): _to_float(r.get("turnover_rate"))
                    for _, r in tb.iterrows()
                }
        except Exception:
            turnover_map = {}

    bars = []
    for _, row in df.iterrows():
        td = str(row["trade_date"])
        pre = _to_float(row.get("pre_close"))
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if (pre and pre > 0) else None
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": float(row["close"]),
            "pre_close": pre,
            "change": _to_float(row.get("change")),
            "pct_chg": float(row.get("pct_chg", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": turnover_map.get(td),
        })
    return bars


async def _hk_kline(ts_code: str, freq: str = "D", adj: str = "",
                    start_date: str = "", end_date: str = "",
                    hist_years: int = 10) -> list[dict]:
    """港股 K 线 (腾讯日线, 不复权); D 直接返回, W/M 由日线聚合。"""
    from . import hk_data_service
    df = await hk_data_service._tencent_kline_df(ts_code)
    if df.empty:
        raise ValueError(f"未获取到 {ts_code} 的港股日线数据。")
    s = start_date or (datetime.now() - timedelta(days=int(hist_years * 365.25) + 10)).strftime("%Y%m%d")
    e = end_date or datetime.now().strftime("%Y%m%d")
    df = df[(df["date"] >= pd.to_datetime(s)) & (df["date"] <= pd.to_datetime(e))].copy()
    if df.empty:
        raise ValueError(f"未获取到 {ts_code} 在 {s}~{e} 的港股K线数据。")
    df = df.sort_values("date").reset_index(drop=True)
    df["trade_date"] = df["date"].dt.strftime("%Y%m%d")
    # 成交额≈量×价 (千元, 与 tushare 口径一致)
    df["amount"] = (df["vol"] * df["close"] / 1000.0)

    # W/M 聚合
    if freq.upper() in ("W", "M"):
        rule = "W" if freq.upper() == "W" else "ME"
        g = df.set_index("date").resample(rule)
        df = pd.DataFrame({
            "trade_date": g["trade_date"].last().values,
            "open": g["open"].first().values,
            "high": g["high"].max().values,
            "low": g["low"].min().values,
            "close": g["close"].last().values,
            "vol": g["vol"].sum().values,
            "amount": g["amount"].sum().values,
        }).dropna(subset=["close"])

    bars = []
    prev = None
    for _, row in df.iterrows():
        td = str(row["trade_date"])
        close = float(row["close"])
        pre = prev if prev is not None else close
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if pre and pre > 0 else None
        pct = (close / pre - 1) * 100.0 if pre and pre > 0 else 0.0
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": close,
            "pre_close": pre,
            "change": close - pre,
            "pct_chg": round(pct, 4),
            "vol": float(row.get("vol") or 0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": None,
        })
        prev = close
    return bars


async def get_stock_detail(ts_code: str, kind: str = "stock", days: int = 250,
                           hist_years: int = 10, date: str = "") -> dict:
    """股票详情聚合: 最多 hist_years 年 K 线 + 52 周高低 + PB/PE/股本/市值 + 分红/股息率。

    hist_years 默认 10 年 (tushare 拉取耗时随年数近似线性, 20 年约 7s / 10 年约 2.3s)。

    date: 指定交易日 (YYYYMMDD) 查看该日行情快照, 空=最新交易日。
    港股 (kind=hk / .HK) 走 _get_hk_stock_detail (东财指标 + 腾讯日线, 金额单位万港元)。
    """
    if kind == "hk" or str(ts_code).endswith(".HK"):
        return await _get_hk_stock_detail(ts_code, days, hist_years, date)
    pro = _init_pro()
    end_date = datetime.now().strftime("%Y%m%d")
    # K 线历史: 最多 hist_years 年 (约 250 交易日/年)
    start = (datetime.now() - timedelta(days=int(hist_years * 365.25) + 10)).strftime("%Y%m%d")
    # 并发拉取: 日线 / daily_basic / 分红 相互独立; 串行会叠加 tushare 延迟 (详情页冷加载 3~6s)
    daily_task = asyncio.create_task(get_daily(ts_code, kind, start_date=start, end_date=end_date))
    basic_task = asyncio.create_task(_get_daily_basic(pro, ts_code, start, end_date))
    div_task = asyncio.create_task(_dividend_latest(pro, ts_code))
    df = await daily_task

    # 选中目标交易日 (指定日期或最新)
    if date:
        target = df[df["trade_date"].astype(str) == str(date)]
        if target.empty:
            raise ValueError(f"交易日 {date} 无行情数据 (可能停牌或未上市)")
        quote_row = target.iloc[0]
    else:
        quote_row = df.iloc[-1]

    recent = df.tail(days).reset_index(drop=True)  # 52 周窗口 (用于高低/最新价)
    last_close = float(quote_row["close"])
    last_date = str(quote_row["trade_date"])
    week52_high = float(recent["high"].max())
    week52_low = float(recent["low"].min())

    # 昨收: 优先 daily.pre_close, 否则取前一交易日收盘
    pre_close = _to_float(quote_row.get("pre_close"))
    if pre_close is None:
        dates = df["trade_date"].astype(str).tolist()
        pos = dates.index(last_date) if last_date in dates else -1
        if pos > 0:
            pre_close = _to_float(df.iloc[pos - 1].get("close"))

    # 当日行情快照 (成交量单位:手, 成交额单位:千元; 盘后成交量 tushare 无此字段; 振幅=(最高-最低)/昨收)
    q_hi = _to_float(quote_row.get("high"))
    q_lo = _to_float(quote_row.get("low"))
    amp_quote = None
    if pre_close and pre_close > 0 and q_hi is not None and q_lo is not None:
        amp_quote = (q_hi - q_lo) / pre_close * 100.0
    quote = {
        "trade_date": last_date,
        "open": _to_float(quote_row.get("open")),
        "high": q_hi,
        "low": q_lo,
        "close": _to_float(quote_row.get("close")),
        "pre_close": pre_close,
        "change": _to_float(quote_row.get("change")),
        "pct_chg": _to_float(quote_row.get("pct_chg")),
        "vol": _to_float(quote_row.get("vol")),
        "amount": _to_float(quote_row.get("amount")),
        "amplitude": amp_quote,
        "after_vol": None,
    }

    # 最新 daily_basic: PB/PE/股本/市值/股息率 + 全历史逐日换手率 (供日期切换取当日换手率)
    pb = pe = pe_ttm = total_share = float_share = total_mv = circ_mv = dv_ratio = None
    turnover_map = {}
    try:
        b = await basic_task
        if b is not None and not b.empty:
            b = b.sort_values("trade_date").reset_index(drop=True)
            turnover_map = dict(
                zip(b["trade_date"].astype(str), b["turnover_rate"].map(_to_float))
            )
            latest = b.iloc[-1]
            pb = _to_float(latest.get("pb"))
            pe = _to_float(latest.get("pe"))
            pe_ttm = _to_float(latest.get("pe_ttm"))
            total_share = _to_float(latest.get("total_share"))   # 万股
            float_share = _to_float(latest.get("float_share"))   # 万股
            total_mv = _to_float(latest.get("total_mv"))         # 万元
            circ_mv = _to_float(latest.get("circ_mv"))           # 万元
            dv_ratio = _to_float(latest.get("dv_ratio"))
    except Exception:
        pass

    # 当日换手率 (来自 daily_basic)
    quote["turnover_rate"] = turnover_map.get(last_date)

    # 分红: 最新分红年度全部已实施每股现金红利之和 (一年多次分红求和)
    try:
        div = await div_task
    except Exception:
        div = None
    div_per_share = div["cash_div"] if div else None
    dividend_end = div["end_date"] if div else ""

    # 股息率: 优先 daily_basic.dv_ratio, 否则 每股分红/最新价
    dividend_yield = dv_ratio
    if div_per_share is not None and last_close:
        dividend_yield = div_per_share / last_close * 100.0

    # K 线 bars (全部历史, 供前端缩放查看最多 20 年; 含快照字段供日期切换本地取用)
    # 用 to_dict("records") 向量化构建 (iterrows 对数千行逐行建 Series 开销大, 是冷加载瓶颈之一)
    records = df.to_dict("records")
    bars = []
    for row in records:
        td = str(row["trade_date"])
        pre = _to_float(row.get("pre_close"))
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if (pre and pre > 0) else None
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": float(row["close"]),
            "pre_close": pre,
            "change": _to_float(row.get("change")),
            "pct_chg": float(row.get("pct_chg", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": turnover_map.get(td),
        })

    return {
        "last_close": last_close,
        "last_date": last_date,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "hist_years": hist_years,
        "bars_count": len(bars),
        "data_source": df.attrs.get("data_source", "tushare"),
        "quote": quote,
        "pb": pb,
        "pe": pe,
        "pe_ttm": pe_ttm,
        "total_share": total_share,
        "float_share": float_share,
        "total_mv": total_mv,
        "circ_mv": circ_mv,
        "dv_ratio": dv_ratio,
        "div_per_share": div_per_share,
        "dividend_end": dividend_end,
        "dividend_yield": dividend_yield,
        "bars": bars,
    }


async def _get_hk_stock_detail(ts_code: str, days: int = 250,
                               hist_years: int = 10, date: str = "") -> dict:
    """港股个股详情 (东财财务指标/分红 + 腾讯日线, 金额单位: 万港元)。

    返回与 A 股 get_stock_detail 同结构 (pb/pe_ttm/total_share万股/total_mv万港元/
    div_per_share港元/dividend_yield%)。港股无 daily_basic, 换手率/流通市值置 None。
    """
    from . import hk_data_service as hkd
    end_date = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(hist_years * 365.25) + 10)).strftime("%Y%m%d")
    df = await get_daily(ts_code, "hk", start_date=start, end_date=end_date)

    # 选中目标交易日
    if date:
        target = df[df["trade_date"].astype(str) == str(date)]
        if target.empty:
            raise ValueError(f"交易日 {date} 无行情数据 (可能停牌或未上市)")
        quote_row = target.iloc[0]
    else:
        quote_row = df.iloc[-1]

    recent = df.tail(days).reset_index(drop=True)  # 52 周窗口
    last_close = float(quote_row["close"])
    last_date = str(quote_row["trade_date"])
    week52_high = float(recent["high"].max())
    week52_low = float(recent["low"].min())
    pre_close = _to_float(quote_row.get("pre_close"))
    q_hi = _to_float(quote_row.get("high"))
    q_lo = _to_float(quote_row.get("low"))
    amp_quote = ((q_hi - q_lo) / pre_close * 100.0) if (pre_close and pre_close > 0 and q_hi is not None and q_lo is not None) else None
    quote = {
        "trade_date": last_date,
        "open": _to_float(quote_row.get("open")),
        "high": q_hi,
        "low": q_lo,
        "close": _to_float(quote_row.get("close")),
        "pre_close": pre_close,
        "change": _to_float(quote_row.get("change")),
        "pct_chg": _to_float(quote_row.get("pct_chg")),
        "vol": _to_float(quote_row.get("vol")),
        "amount": _to_float(quote_row.get("amount")),
        "amplitude": amp_quote,
        "turnover_rate": None,
        "after_vol": None,
    }

    # 东财港股财务指标 (最新财年) → 估值
    try:
        m = await hkd.stock_metrics(ts_code)
    except Exception:
        m = {}
    fina = m.get("fina") or {}
    latest_year = max(fina.keys()) if fina else 0
    f = fina.get(latest_year) or {}
    pb = f.get("pb")
    pe_ttm = f.get("pe_ttm")
    pe = None
    shares = f.get("issued_shares")
    total_share = (shares / 10000.0) if shares else None  # 股 → 万股 (与 A 股 total_share 口径一致)
    total_mv = f.get("total_mv_wan")                        # 万港元
    circ_mv = None

    # 分红/股息率: 最新财政年度每股现金分红 (港元) / 最新收盘价
    dividends = m.get("dividends") or {}
    div_per_share = dividends.get(latest_year)
    dividend_end = f"{latest_year}-12-31" if latest_year else ""
    dividend_yield = (div_per_share / last_close * 100.0) if (div_per_share and last_close) else None
    dv_ratio = dividend_yield

    # K 线 bars (全部历史)
    bars = []
    for _, row in df.iterrows():
        td = str(row["trade_date"])
        pre = _to_float(row.get("pre_close"))
        hi = float(row["high"])
        lo = float(row["low"])
        amp = ((hi - lo) / pre * 100.0) if (pre and pre > 0) else None
        bars.append({
            "date": td,
            "open": float(row["open"]),
            "high": hi,
            "low": lo,
            "close": float(row["close"]),
            "pre_close": pre,
            "change": _to_float(row.get("change")),
            "pct_chg": float(row.get("pct_chg", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": _to_float(row.get("amount")),
            "amplitude": amp,
            "turnover_rate": None,
        })

    return {
        "last_close": last_close,
        "last_date": last_date,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "hist_years": hist_years,
        "bars_count": len(bars),
        "data_source": "hk" if str(ts_code).endswith(".HK") else "tushare",
        "quote": quote,
        "pb": pb,
        "pe": pe,
        "pe_ttm": pe_ttm,
        "total_share": total_share,
        "float_share": None,
        "total_mv": total_mv,
        "circ_mv": circ_mv,
        "dv_ratio": dv_ratio,
        "div_per_share": div_per_share,
        "dividend_end": dividend_end,
        "dividend_yield": dividend_yield,
        "bars": bars,
    }


# ---------------------------------------------------------------------------
# 我的股票快照 (轻量行情, 供自选股列表)
# ---------------------------------------------------------------------------

# 快照 TTL 缓存 (15 分钟): 避免每次列表刷新对每只自选股重复打 tushare (daily/daily_basic/dividend)
_SNAPSHOT_CACHE: dict[str, tuple[float, dict]] = {}
# 快照 daily_basic 独立缓存 (key=ts_code): 自选股批量加载时复用, 避免每只重复拉 daily_basic
_SNAPSHOT_BASIC_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_SNAPSHOT_TTL = 15 * 60


async def _get_snapshot_basic(pro, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """快照用的 daily_basic (带 15 分钟缓存, 与 _SNAPSHOT_CACHE 同生命周期)。

    注意: 与 _get_daily_basic (详情页 20 年窗口) 缓存独立, 避免窗口污染。
    """
    hit = _SNAPSHOT_BASIC_CACHE.get(ts_code)
    if hit and time.time() - hit[0] < _SNAPSHOT_TTL:
        return hit[1]
    try:
        b = await pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=end_date,
                                  fields="trade_date,close,pb,pe,pe_ttm,total_mv,circ_mv,dv_ratio")
    except Exception:
        b = None
    _SNAPSHOT_BASIC_CACHE[ts_code] = (time.time(), b)
    return b


async def get_stock_snapshot(ts_code: str, kind: str = "stock", days: int = 250) -> dict:
    """轻量行情快照 (我的股票列表用): 最新收盘/涨跌幅 + 52周高低 + PE/PB + 总市值 + 股息率 + 每股分红。

    不取 20 年 K 线, 比 get_stock_detail 轻量; 股息率为 TTM 口径 (最新分红年度全年分红 / 最新收盘价)。
    结果带 15 分钟 TTL 缓存。
    """
    cache_key = f"{ts_code}:{kind}"
    _now = time.time()
    _hit = _SNAPSHOT_CACHE.get(cache_key)
    if _hit and _now - _hit[0] < _SNAPSHOT_TTL:
        return dict(_hit[1])

    pro = _init_pro()
    end_date = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(days * 1.6) + 30)).strftime("%Y%m%d")
    df = await get_daily(ts_code, kind, start_date=start, end_date=end_date)
    last = df.iloc[-1]
    last_close = float(last["close"])
    last_date = str(last["trade_date"])
    pct_chg = _to_float(last.get("pct_chg"))
    recent = df.tail(days)
    week52_high = float(recent["high"].max())
    week52_low = float(recent["low"].min())

    pb = pe = pe_ttm = total_mv = circ_mv = dv_ratio = None
    try:
        b = await _get_snapshot_basic(pro, ts_code, start, end_date)
        if b is not None and not b.empty:
            b = b.sort_values("trade_date").iloc[-1]
            pb = _to_float(b.get("pb"))
            pe = _to_float(b.get("pe"))
            pe_ttm = _to_float(b.get("pe_ttm"))
            total_mv = _to_float(b.get("total_mv"))   # 万元
            circ_mv = _to_float(b.get("circ_mv"))     # 万元
            dv_ratio = _to_float(b.get("dv_ratio"))
    except Exception:
        pass

    div = await _dividend_latest(pro, ts_code)
    div_per_share = div["cash_div"] if div else None
    dividend_yield = None
    if div_per_share is not None and last_close:
        dividend_yield = div_per_share / last_close * 100.0

    result = {
        "ts_code": ts_code,
        "name": None,   # 由调用方 (resolve_code) 补齐
        "last_close": last_close,
        "last_date": last_date,
        "pct_chg": pct_chg,
        "pb": pb,
        "pe": pe,
        "pe_ttm": pe_ttm,
        "total_mv": total_mv,
        "circ_mv": circ_mv,
        "dv_ratio": dv_ratio,
        "div_per_share": div_per_share,
        "dividend_yield": dividend_yield,
        "week52_high": week52_high,
        "week52_low": week52_low,
    }
    # 缓存副本, 返回独立对象 (调用方会补 name/industry 等字段, 不改缓存)
    _SNAPSHOT_CACHE[cache_key] = (time.time(), dict(result))
    return result


# ---------------------------------------------------------------------------
# 自选股批量快照 (加速: trade_date 批量接口取最新行情/估值, 52周高低/分红逐只带缓存)
# ---------------------------------------------------------------------------

# 全市场最新交易日行情/指标 (trade_date 批量, 15 分钟缓存)
_LATEST_DAILY_CACHE: dict[str, tuple[float, dict]] = {}
_LATEST_BASIC_CACHE: dict[str, tuple[float, dict]] = {}
_LATEST_TTL = 15 * 60


async def _latest_trade_date(pro) -> str:
    """探测最近交易日 (用 000001 日线最新 trade_date)。"""
    try:
        probe = await pro.daily(ts_code="000001.SZ", fields="trade_date,close")
        if probe is not None and not probe.empty:
            return str(probe.sort_values("trade_date").iloc[-1]["trade_date"])
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")


async def _latest_daily_map(pro) -> dict[str, dict]:
    """全市场最新交易日 daily → {ts_code: {close, pre_close, pct_chg}} (15min 缓存)。

    一次调用替代 N 只自选股逐只拉日线, 是自选股列表加速的关键。
    """
    now = time.time()
    hit = _LATEST_DAILY_CACHE.get("all")
    if hit and now - hit[0] < _LATEST_TTL:
        return hit[1]
    m: dict[str, dict] = {}
    try:
        td = await _latest_trade_date(pro)
        df = await pro.daily(trade_date=td, fields="ts_code,close,pre_close,pct_chg")
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                m[str(r["ts_code"])] = {
                    "close": _to_float(r.get("close")),
                    "pre_close": _to_float(r.get("pre_close")),
                    "pct_chg": _to_float(r.get("pct_chg")),
                }
    except Exception:
        pass
    _LATEST_DAILY_CACHE["all"] = (now, m)
    return m


async def _latest_basic_map(pro) -> dict[str, dict]:
    """全市场最新 daily_basic → {ts_code: {pb, pe, pe_ttm, total_mv, circ_mv, dv_ttm}} (15min 缓存)。"""
    now = time.time()
    hit = _LATEST_BASIC_CACHE.get("all")
    if hit and now - hit[0] < _LATEST_TTL:
        return hit[1]
    m: dict[str, dict] = {}
    try:
        td = await _latest_trade_date(pro)
        df = await pro.daily_basic(trade_date=td, fields="ts_code,pb,pe,pe_ttm,total_mv,circ_mv,dv_ttm")
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                m[str(r["ts_code"])] = {
                    "pb": _to_float(r.get("pb")),
                    "pe": _to_float(r.get("pe")),
                    "pe_ttm": _to_float(r.get("pe_ttm")),
                    "total_mv": _to_float(r.get("total_mv")),
                    "circ_mv": _to_float(r.get("circ_mv")),
                    "dv_ttm": _to_float(r.get("dv_ttm")),
                }
    except Exception:
        pass
    _LATEST_BASIC_CACHE["all"] = (now, m)
    return m


async def get_snapshots_batch(ts_codes: list[str], days: int = 250) -> dict[str, dict]:
    """批量股票快照 (我的股票列表用): A股最新行情/估值用 trade_date 批量接口 (一次全市场),
    52 周高低 + 每股分红逐只 (带缓存, 并发上限控 tushare 限频); 港股/基金回退逐只 get_stock_snapshot。

    相比逐只 get_stock_snapshot (每只 3 次 tushare 调用: daily/daily_basic/dividend),
    批量接口把 N 次 daily_basic 与最新行情降为 2 次全市场调用, 大幅降低自选股列表
    首次加载耗时与 tushare 限频压力。返回 {ts_code: snap | None}, snap 结构同 get_stock_snapshot。
    """
    a_codes = [c for c in ts_codes if str(c).endswith((".SH", ".SZ"))]
    rest = [c for c in ts_codes if c not in a_codes]
    out: dict[str, dict] = {}
    pro = _init_pro()
    dmap = await _latest_daily_map(pro)
    bmap = await _latest_basic_map(pro)
    end_date = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(days * 1.6) + 30)).strftime("%Y%m%d")
    sem = asyncio.Semaphore(8)

    async def _one(code: str) -> dict | None:
        async with sem:
            try:
                df = await get_daily(code, "stock", start_date=start, end_date=end_date)
                last = df.iloc[-1]
                recent = df.tail(days)
                d = dmap.get(code) or {}
                b = bmap.get(code) or {}
                last_close = float(d["close"]) if d.get("close") is not None else float(last["close"])
                div = await _dividend_latest(pro, code)
                div_per_share = div["cash_div"] if div else None
                dv_ttm = b.get("dv_ttm")
                dividend_yield = dv_ttm if dv_ttm is not None else (
                    div_per_share / last_close * 100.0 if (div_per_share and last_close) else None)
                return {
                    "ts_code": code,
                    "last_close": last_close,
                    "last_date": str(last["trade_date"]),
                    "pct_chg": d.get("pct_chg") if d.get("pct_chg") is not None else _to_float(last.get("pct_chg")),
                    "pb": b.get("pb"),
                    "pe": b.get("pe"),
                    "pe_ttm": b.get("pe_ttm"),
                    "total_mv": b.get("total_mv"),
                    "circ_mv": b.get("circ_mv"),
                    "dv_ratio": dv_ttm,
                    "div_per_share": div_per_share,
                    "dividend_yield": dividend_yield,
                    "week52_high": float(recent["high"].max()),
                    "week52_low": float(recent["low"].min()),
                }
            except Exception:
                return None

    a_results = await asyncio.gather(*(_one(c) for c in a_codes))
    for code, snap in zip(a_codes, a_results):
        out[code] = snap

    for code in rest:
        try:
            info = await resolve_code(code)
            out[code] = await get_stock_snapshot(info["ts_code"], kind=info["kind"])
        except Exception:
            out[code] = None
    return out


# ---------------------------------------------------------------------------
# 基本面选股 (资产负债率 / ROE / 分红率)
# ---------------------------------------------------------------------------


def _to_float(v) -> float | None:
    """安全转 float, 无效值返回 None。"""
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


async def _fina_latest(pro, ts_code: str, period: str = "") -> dict | None:
    """获取单只股票最新一期 (或指定报告期) 财务指标。

    未指定 period 时优先取最新年报 (end_date 以 1231 结尾), 保证 ROE 为全年口径;
    若无年报数据则退回最新一期。返回含 roe/debt_to_assets/dt_eps/end_date 的 dict。
    """
    try:
        if period:
            df = await pro.fina_indicator(ts_code=ts_code, period=period)
        else:
            df = await pro.fina_indicator(ts_code=ts_code)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("end_date", ascending=False).reset_index(drop=True)
    if not period:
        annual = df[df["end_date"].astype(str).str.endswith("1231")]
        if not annual.empty:
            df = annual
    row = df.iloc[0]
    return {
        "end_date": str(row.get("end_date") or ""),
        "debt_to_assets": _to_float(row.get("debt_to_assets")),
        "roe": _to_float(row.get("roe")),
        "dt_eps": _to_float(row.get("dt_eps")),
        "fcff": _to_float(row.get("fcff")),
        "gross_margin": _to_float(row.get("grossprofit_margin")),
    }


async def _annual_div_per_share(pro, ts_code: str, year: int) -> float | None:
    """某分红年度 (end_date 年份==year) 全部已实施每股现金红利之和 (元)。

    tushare dividend 的 end_date 可为年内各期 (0630 中期/0930 三季/1231 年度),
    同一方案又有 预案/股东大会/实施 多条流程记录。许多公司一年多次分红 (中期+
    末期等), 若只取单个 end_date 的单条'实施'记录会严重低估全年每股分红与股息率。
    故按 end_date 年份聚合所有'实施'记录 cash_div 求和; 无实施记录时退回该年
    cash_div 最大值。
    """
    try:
        dv = await pro.dividend(ts_code=ts_code)
    except Exception:
        return None
    if dv is None or dv.empty:
        return None
    yearly = dv[dv["end_date"].astype(str).str.startswith(str(year))].copy()
    if yearly.empty or "cash_div" not in yearly.columns:
        return None
    yearly["_cash"] = pd.to_numeric(yearly["cash_div"], errors="coerce")
    impl = yearly[yearly["div_proc"] == "实施"]
    if not impl.empty:
        # tushare 偶发同一 end_date 有重复'实施'记录 (如茅台 20251231 两条相同
        # 28.02423), 按 (end_date, cash_div) 去重后再求和, 避免重复计入
        impl = impl.drop_duplicates(subset=["end_date", "cash_div"])
        total = float(impl["_cash"].sum(skipna=True))
        return total if total > 0 else None
    mx = float(yearly["_cash"].max(skipna=True))
    return mx if mx > 0 else None


def _calc_latest_dividend(dv: pd.DataFrame) -> dict | None:
    """从分红明细 df (含 end_date/div_proc/cash_div) 计算最近分红年度每股现金红利之和 (元)。

    按 end_date 年份聚合'实施'记录求和 (解决一年多次分红低估), 取最近有实施记录
    的年份; 若无实施记录则退回全部记录中 cash_div 最大那条。
    返回 {"end_date": "YYYY1231", "cash_div": 全年每股现金红利}。
    """
    if dv is None or dv.empty:
        return None
    dv = dv.copy()
    if "cash_div" not in dv.columns:
        return None
    dv["_cash"] = pd.to_numeric(dv["cash_div"], errors="coerce")
    dv["_yr"] = pd.to_numeric(dv["end_date"].astype(str).str[:4], errors="coerce")
    impl = dv[(dv["div_proc"] == "实施") & (dv["_yr"].notna())]
    if not impl.empty:
        # 同 end_date 重复记录 (tushare 偶发) 去重后再求和
        impl = impl.drop_duplicates(subset=["end_date", "cash_div"])
        latest_yr = int(impl["_yr"].max())
        sel = impl[impl["_yr"] == latest_yr]
        total = float(sel["_cash"].sum(skipna=True))
        return {"end_date": f"{latest_yr}1231", "cash_div": total if total > 0 else None}
    # 无实施记录: 退回现金红利最大的一条
    dv = dv.dropna(subset=["_cash"])
    if dv.empty:
        return None
    best = dv.loc[dv["_cash"].idxmax()]
    val = float(best["_cash"])
    return {"end_date": str(best.get("end_date") or ""), "cash_div": val if val > 0 else None}


async def _backfill_dividend_one(ts_code: str, dv: pd.DataFrame) -> None:
    """把 tushare dividend 明细回填到本地 stock_dividends (幂等 upsert)。"""
    from . import pg_service
    symbol = str(ts_code)  # 完整 ts_code (带后缀)
    rows = []
    for _, r in dv.iterrows():
        rows.append((symbol, str(r.get("end_date") or "")[:10], str(r.get("div_proc") or "")[:16],
                     _to_float(r.get("cash_div")), _to_float(r.get("stk_div")),
                     _to_float(r.get("stk_bo_rate")), str(r.get("ann_date") or "")[:10],
                     str(r.get("record_date") or "")[:10], str(r.get("ex_date") or "")[:10],
                     str(r.get("pay_date") or "")[:10]))
    if rows:
        await pg_service.upsert_dividend_rows(rows)


async def _dividend_latest(pro, ts_code: str) -> dict | None:
    """获取单只股票最近分红年度每股现金红利之和 (带 1h TTL 缓存)。"""
    hit = _DIVIDEND_CACHE.get(ts_code)
    if hit and (time.time() - hit[0]) < _TTL_DIVIDEND:
        return hit[1]
    result = await _dividend_latest_uncached(pro, ts_code)
    _DIVIDEND_CACHE[ts_code] = (time.time(), result)
    return result


async def _dividend_latest_uncached(pro, ts_code: str) -> dict | None:
    """(无缓存) 获取最近分红年度每股现金红利之和: 优先本地 pg stock_dividends, 否则 tushare 并回填。"""
    from . import pg_service
    symbol = str(ts_code)  # 完整 ts_code (带后缀)
    try:
        rows = await pg_service.query_dividends(symbol)
    except Exception:
        rows = []
    if rows:
        return _calc_latest_dividend(pd.DataFrame(rows))
    try:
        dv = await pro.dividend(ts_code=ts_code)
    except Exception:
        return None
    if dv is None or dv.empty:
        return None
    # 回填本地 pg (后台异步, 不阻塞本次响应)
    try:
        asyncio.create_task(_backfill_dividend_one(ts_code, dv))
    except Exception:
        pass
    return _calc_latest_dividend(dv)


# ---------------------------------------------------------------------------
# 本地日线/财务 持久化回填 (目标列表: 我的股票 ∪ 策略Hub股票 ∪ ETF)
# ---------------------------------------------------------------------------

def _classify_ts_code(ts_code: str) -> str:
    """判断 ts_code 类型: stock / fund (与 _is_fund_code 一致)。"""
    return "fund" if _is_fund_code(str(ts_code)) else "stock"


async def backfill_daily_bars(targets: list[dict], years: int = 10,
                              concurrency: int = 4) -> dict:
    """把目标股票/ETF 最近 N 年日线(原始价+复权因子+换手率)同步到本地 stock_daily_bars。

    targets: [{"ts_code": "600036.SH", "kind": "stock"|"fund"}, ...]
    返回 {"ok": 同步成功数, "skip": 跳过/失败数, "rows": 写入行数, "errors": [...]}。
    供 scripts/sync_local_bars.py 与每日定时任务调用 (幂等 upsert, 可重复续跑)。
    """
    from . import pg_service
    await pg_service.init_alpha158_schema()  # 确保日线表存在 (含 kind 列)
    await pg_service.init_daily_basic_schema()  # 估值/换手率表
    await pg_service.init_dividend_schema()     # 分红明细表
    end_date = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(years * 365.25) + 10)).strftime("%Y%m%d")
    sem = asyncio.Semaphore(concurrency)
    summary = {"ok": 0, "skip": 0, "rows": 0, "errors": []}

    def _d(v):
        if v is None:
            return None
        s = str(v)[:10]
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None

    def _n(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if f != f else f
        except (TypeError, ValueError):
            return None

    async def _one(t: dict):
        ts_code = t["ts_code"]
        kind = t.get("kind") or _classify_ts_code(ts_code)
        symbol = str(ts_code)  # 与 alpha158 约定一致: symbol = 完整 ts_code (带后缀)
        async with sem:
            try:
                pro = _init_pro()
                df = await pro.daily(ts_code=ts_code, start_date=start, end_date=end_date)
                if df is None or df.empty:
                    if kind == "fund":
                        df = await pro.fund_daily(ts_code=ts_code, start_date=start, end_date=end_date)
                    else:
                        df = await pro.fund_daily(ts_code=ts_code, start_date=start, end_date=end_date)
                if df is None or df.empty:
                    summary["skip"] += 1
                    return
                df = df.sort_values("trade_date").reset_index(drop=True)
                # 复权因子 (ETF 同样支持 adj_factor; 失败则为空字典)
                adj_map: dict = {}
                try:
                    af = await pro.adj_factor(ts_code=ts_code, start_date=start, end_date=end_date)
                    if af is not None and not af.empty:
                        adj_map = {str(r["trade_date"]): float(r["adj_factor"])
                                   for _, r in af.iterrows()}
                except Exception:
                    adj_map = {}
                # 估值/换手率 (daily_basic, ETF 可能无 → 空) + 一并入库 stock_daily_basic
                tr_map: dict = {}
                basic_rows: list = []
                try:
                    tb = await pro.daily_basic(
                        ts_code=ts_code, start_date=start, end_date=end_date,
                        fields="trade_date,close,pb,pe,pe_ttm,total_share,float_share,total_mv,circ_mv,dv_ratio,dv_ttm,turnover_rate")
                    if tb is not None and not tb.empty:
                        tr_map = {str(r["trade_date"]): _n(r.get("turnover_rate"))
                                  for _, r in tb.iterrows()}
                        for _, r in tb.iterrows():
                            td = str(r["trade_date"])
                            basic_rows.append((symbol, _d(td), _n(r.get("close")),
                                               _n(r.get("pb")), _n(r.get("pe")),
                                               _n(r.get("pe_ttm")),
                                               _n(r.get("total_share")), _n(r.get("float_share")),
                                               _n(r.get("total_mv")), _n(r.get("circ_mv")),
                                               _n(r.get("dv_ratio")), _n(r.get("dv_ttm")),
                                               _n(r.get("turnover_rate"))))
                except Exception:
                    tr_map = {}
                # 分红明细 → 一并入库 stock_dividends
                div_rows: list = []
                try:
                    dv = await pro.dividend(ts_code=ts_code)
                    if dv is not None and not dv.empty:
                        for _, r in dv.iterrows():
                            div_rows.append((symbol, str(r.get("end_date") or "")[:10],
                                             str(r.get("div_proc") or "")[:16],
                                             _n(r.get("cash_div")), _n(r.get("stk_div")),
                                             _n(r.get("stk_bo_rate")),
                                             str(r.get("ann_date") or "")[:10],
                                             str(r.get("record_date") or "")[:10],
                                             str(r.get("ex_date") or "")[:10],
                                             str(r.get("pay_date") or "")[:10]))
                except Exception:
                    div_rows = []

                rows = []
                for _, r in df.iterrows():
                    td = str(r["trade_date"])
                    vol = _n(r.get("vol"))
                    amount = _n(r.get("amount"))
                    vwap = (amount * 10.0 / vol) if (vol and amount and vol > 0) else None
                    rows.append((symbol, kind, _d(td), _n(r.get("open")), _n(r.get("high")),
                                 _n(r.get("low")), _n(r.get("close")), _n(r.get("pre_close")),
                                 _n(r.get("pct_chg")), vol, amount, vwap,
                                 adj_map.get(td), tr_map.get(td)))
                n = await pg_service.upsert_daily_bars(rows)
                if basic_rows:
                    await pg_service.upsert_daily_basic_rows(basic_rows)
                if div_rows:
                    await pg_service.upsert_dividend_rows(div_rows)
                summary["ok"] += 1
                summary["rows"] += n
            except Exception as e:
                summary["skip"] += 1
                summary["errors"].append({"ts_code": ts_code, "msg": str(e)[:120]})

    await asyncio.gather(*(_one(t) for t in targets))
    return summary


async def backfill_missing_financial(targets: list[dict], years: list[int] | None = None) -> dict:
    """为目标 A股 增量补齐财务数据 (financial_data): 检查本地已入库年份, 只补缺失年份。

    targets: [{"ts_code":..., "kind":...}]。ETF/港股跳过。
    当年 (当前年) 年报通常未披露, 不强制补齐 (避免每夜空转)。
    返回 {"ok": 补齐数, "skip": 跳过数, "errors": [...]}。
    """
    from . import pg_service
    cur = datetime.now().year
    if not years:
        years = list(range(cur - 8, cur + 1))
    summary = {"ok": 0, "skip": 0, "errors": []}
    for t in targets:
        ts_code = t["ts_code"]
        kind = t.get("kind") or _classify_ts_code(ts_code)
        if kind != "stock" or str(ts_code).endswith(".HK"):
            summary["skip"] += 1
            continue
        try:
            have = set(await pg_service.financial_years(ts_code))
            # 缺失年份 = 目标年份中未入库且非当年的 (当年年报未披露, 不强制)
            missing = [y for y in years if y != cur and y not in have]
            if not missing:
                summary["skip"] += 1
                continue
            from . import caibao_service
            n = await caibao_service.sync_stock_financial(ts_code, missing)
            if n:
                summary["ok"] += 1
            else:
                summary["skip"] += 1
        except Exception as e:
            summary["skip"] += 1
            summary["errors"].append({"ts_code": ts_code, "msg": str(e)[:120]})
    return summary
