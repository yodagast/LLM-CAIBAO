"""单只股票四策略回测 Web 系统 (FastAPI)。

启动:
    cd LLM-CAIBAO
    uvicorn api.main:app --reload --port 8000

打开 http://127.0.0.1:8000 使用回测页面。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import backtest_engine, band_service, caibao_service, data_service, daily_recommend_service
from . import etf_service, fundamental_service, hk_data_service, hk_fundamental_service
from . import hk_redlowvol_service, pg_service, redlowvol_service

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="单股四策略回测系统",
    description="基于 tushare 数据源, 对单只股票运行 买入持有 / 限价买入持有 / 区间交易 / 低价买入 四种策略回测。",
    version="1.3.0",
)


@app.on_event("startup")
def _startup() -> None:
    """启动时确保 PG 表结构存在 (失败不阻塞服务启动)。"""
    try:
        pg_service.init_schema()
        pg_service.init_fundamental_schema()
        pg_service.init_financial_schema()
        pg_service.init_daily_rec_schema()
        pg_service.init_my_stocks_schema()
        pg_service.init_hk_rlv_schema()
        pg_service.init_hk_fundamental_schema()
        pg_service.init_etf_schema()
    except Exception:
        pass

# 静态资源 (前端页面)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------

class BacktestRequest(BaseModel):
    ts_code: str = Field(..., description="股票代码, 如 000858.SZ 或 000858")
    buy_price: float = Field(..., gt=0, description="买入价格")
    sell_price: float = Field(..., gt=0, description="卖出价格")
    stop_price: float = Field(..., gt=0, description="止损价格")
    lookback_days: int = Field(20, ge=2, le=250, description="低价策略 N 日窗口")
    start_date: str = Field("20170101", description="起始日期 YYYYMMDD")
    end_date: str = Field("", description="结束日期 YYYYMMDD, 空表示最新")
    initial_capital: float = Field(100000.0, gt=0, description="初始资金")
    gain_threshold: float = Field(20.0, gt=0, description="低价策略涨幅卖出阈值 (%)")


class ScreenRequest(BaseModel):
    """基本面选股请求 (ROE 杜邦拆分)。"""
    industry: str = Field("", description="行业名称(东财分类), 空表示全市场")
    years: list[int] = Field(..., min_length=1, max_length=15,
                             description="年度列表, 如 [2024,2025]")
    sort_by: str = Field("roe", description="排序字段 (ROE/净利润率/毛利率等)")
    order: str = Field("desc", description="排序方向: asc / desc")
    filters: dict = Field(default_factory=dict,
                          description="筛选条件 {字段: {min: x, max: y}}, 如 {'roe': {'min': 15}, 'debt_to_assets': {'max': 60}}")
    max_stocks: int = Field(6000, ge=1, le=20000, description="最多扫描股票数(全市场时生效, 全市场约5536只)")
    limit: int = Field(1000, ge=1, le=2000, description="返回数量上限")


class RedLowVolRequest(BaseModel):
    """红利低波选股请求。"""
    industry: str = Field("", description="行业名称(东财分类), 空表示全市场")
    years: list[int] = Field(..., min_length=1, max_length=15,
                             description="年度列表, 如 [2020,2021,2022,2023,2024,2025]")
    sort_by: str = Field("dividend_yield", description="排序字段 (名称/股息率/波动率/每股分红/自由现金流等)")
    order: str = Field("desc", description="排序方向: asc / desc")
    filters: dict = Field(default_factory=dict,
                          description="筛选条件 {字段: {min: x, max: y}}, 如 {'dividend_yield': {'min': 5}}")
    max_stocks: int = Field(6000, ge=1, le=20000, description="同步时最多扫描股票数(全市场约5536只)")
    limit: int = Field(500, ge=1, le=1000, description="返回数量上限")


class HkScreenRequest(BaseModel):
    """港股基本面选股请求 (ROE 杜邦拆分)。"""
    industry: str = Field("", description="行业名称(东财港股行业, 如 银行/软件服务), 空表示全市场")
    years: list[int] = Field(..., min_length=1, max_length=15,
                             description="年度列表, 如 [2024,2025]")
    sort_by: str = Field("roe", description="排序字段 (ROE/净利润率/毛利率等)")
    order: str = Field("desc", description="排序方向: asc / desc")
    filters: dict = Field(default_factory=dict,
                          description="筛选条件 {字段: {min: x, max: y}}, 如 {'roe': {'min': 15}, 'debt_to_assets': {'max': 60}}")
    max_stocks: int = Field(3000, ge=1, le=10000, description="最多扫描股票数(全市场港股约2782只)")
    limit: int = Field(1000, ge=1, le=2000, description="返回数量上限")


class HkRedLowVolRequest(BaseModel):
    """港股红利低波选股请求。"""
    industry: str = Field("", description="行业名称(东财港股行业), 空表示全市场")
    years: list[int] = Field(..., min_length=1, max_length=15,
                             description="年度列表, 如 [2023,2024,2025]")
    sort_by: str = Field("dividend_yield", description="排序字段")
    order: str = Field("desc", description="排序方向: asc / desc")
    filters: dict = Field(default_factory=dict,
                          description="筛选条件 {字段: {min: x, max: y}}")
    max_stocks: int = Field(3000, ge=1, le=10000, description="同步时最多扫描股票数(全市场约2782只)")
    limit: int = Field(500, ge=1, le=1000, description="返回数量上限")


class MyStockRequest(BaseModel):
    """我的股票 (自选股) 请求。"""
    ts_code: str = Field(..., description="股票代码, 如 000858 或 000858.SZ")


class BandOptimizeRequest(BaseModel):
    """区间交易参数估算请求。"""
    ts_code: str = Field(..., description="股票代码, 如 000858.SZ 或 000858")
    start_date: str = Field("20170101", description="历史区间起始日期 YYYYMMDD")
    end_date: str = Field("", description="历史区间结束日期 YYYYMMDD, 空=最新")
    initial_capital: float = Field(100000.0, gt=0, description="初始资金")
    min_sharpe: float = Field(1.0, ge=0, le=10, description="目标夏普比率下限")
    objective: str = Field("balanced",
                           description="优化目标: return 收益优先 / annual 年化收益优先 / "
                                       "sharpe 夏普优先 / drawdown 回撤最小 / calmar 卡玛优先 / "
                                       "balanced 综合平衡")
    max_trades: int = Field(100, ge=1, le=2000,
                            description="交易次数上限 (每笔=买入→卖出完整周期), 超过则淘汰该参数; 默认 100")


class CaibaoRequest(BaseModel):
    """财报分析请求。"""
    ts_code: str = Field(..., description="股票代码, 如 600036.SH 或 600036")
    start_year: int = Field(2022, ge=2000, le=2100, description="起始年份")
    end_year: int = Field(2024, ge=2000, le=2100, description="结束年份")
    use_llm: bool = Field(False, description="是否使用 LLM 深度分析 (默认 False=基于 TUSHARE 财报数据规则化分析; True 且 .env 配置了 API Key 时用 LLM)")


class DailyRecRequest(BaseModel):
    """每日公司推荐扫描请求。"""
    ts_codes: str = Field("", description="指定代码(逗号分隔), 空=按行业或全市场")
    industry: str = Field("", description="行业名称(东财分类), 空=全市场")
    limit: int = Field(0, ge=0, le=20000, description="限制计算数量(0=不限, 全市场约5200只耗时数小时)")
    objective: str = Field("balanced", description="优化目标")
    min_sharpe: float = Field(1.0, ge=0, le=10, description="目标夏普下限")
    max_trades: int = Field(100, ge=1, le=2000, description="交易次数上限")
    start_date: str = Field("20170101", description="回测起始日期 YYYYMMDD")
    end_date: str = Field("", description="回测结束日期 YYYYMMDD, 空=最新")
    use_cache: bool = Field(True, description="行业扫描时若 pgsql 已有当天数据则直接读取(不重复计算)")
    method: str = Field("band", description="推荐方法: band=区间交易(方法1), dividend=红利低波动态股息率(方法2)")
    min_dy_ttm: float = Field(3.0, ge=0, le=100, description="方法2: 动态股息率(股息率-TTM)下限 %")
    year_min: int | None = Field(None, ge=2000, le=2100, description="方法2: 年份区间下限 (空=最新)")
    year_max: int | None = Field(None, ge=2000, le=2100, description="方法2: 年份区间上限 (空=最新/不限)")
    payout_min: float | None = Field(None, ge=0, le=1000, description="方法2: 分红率下限 % (可选)")
    payout_max: float | None = Field(None, ge=0, le=1000, description="方法2: 分红率上限 % (可选)")
    roe_min: float | None = Field(None, ge=-100, le=1000, description="方法2: ROE下限 % (可选)")
    roe_max: float | None = Field(None, ge=-100, le=1000, description="方法2: ROE上限 % (可选)")


class EtfScreenRequest(BaseModel):
    """ETF 筛选请求。"""
    keyword: str = Field("", description="名称/代码关键字, 空=全部")
    fund_type: str = Field("", description="基金类型 (如 股票型/债券型/商品型/跨境型/货币型), 空=全部")
    min_scale: float | None = Field(None, ge=0, description="基金规模 ≥ (亿元, 可选)")
    max_m_fee: float | None = Field(None, ge=0, le=10, description="管理费率 ≤ (% , 可选)")
    max_c_fee: float | None = Field(None, ge=0, le=10, description="托管费率 ≤ (% , 可选)")
    min_amount_20: float | None = Field(None, ge=0, description="近20日均成交额 ≥ (万元, 可选)")
    max_premium: float | None = Field(None, ge=0, le=50, description="折溢价 |≤| (% , 可选)")
    min_pos52: float | None = Field(None, ge=0, le=1, description="52周位置 ≥ (0~1, 可选)")
    max_pos52: float | None = Field(None, ge=0, le=1, description="52周位置 ≤ (0~1, 可选)")
    sort_by: str = Field("scale", description="排序字段 (scale/avg_amount_20/premium/m_fee/total_fee/pos52/close 等)")
    order: str = Field("desc", description="排序方向: asc / desc")
    limit: int = Field(300, ge=1, le=2000, description="最多返回数量 (受基础过滤后逐只计算)")
    refresh: bool = Field(False, description="是否强制刷新 tushare 缓存 (默认用 15 分钟缓存)")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/stock/search")
def stock_search(keyword: str = Query("", description="代码或名称关键字"),
                 limit: int = Query(20, ge=1, le=50)) -> dict:
    """股票关键字搜索 (前端联想)。"""
    try:
        items = data_service.search_stock(keyword, limit)
    except Exception as e:  # token 未配置等
        raise HTTPException(status_code=500, detail=str(e))
    return {"items": items}


@app.get("/api/industry/search")
def industry_search(keyword: str = Query("", description="行业关键字, 空=热门行业"),
                    limit: int = Query(20, ge=1, le=50)) -> dict:
    """行业模糊搜索: 返回匹配行业及股票数量 (前端候选推荐)。"""
    try:
        items = data_service.search_industries(keyword, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"items": items}


@app.get("/api/stock/{code}")
def stock_info(code: str) -> dict:
    """解析股票代码, 返回基本信息。"""
    try:
        return data_service.resolve_code(code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/quote/{code}")
def quote(code: str, days: int = Query(120, ge=10, le=500)) -> dict:
    """获取股票/ETF 最近 N 个交易日行情 (K线), 供前端绘制行情曲线。"""
    try:
        info = data_service.resolve_code(code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        df = data_service.get_quote(info["ts_code"], kind=info["kind"], days=days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取行情数据失败: {e}")

    bars = []
    for _, row in df.iterrows():
        bars.append({
            "date": row["trade_date"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "pct_chg": float(row.get("pct_chg", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
        })

    return {
        "info": info,
        "days": len(bars),
        "start": bars[0]["date"] if bars else "",
        "end": bars[-1]["date"] if bars else "",
        "bars": bars,
    }


@app.get("/api/stock/detail/{code}")
def stock_detail(code: str, date: str = Query("", description="交易日 YYYYMMDD, 空=最新")) -> dict:
    """股票详情: K线 + 行情快照(可指定日期) + 52周高低 + PB/PE/股本/市值 + 分红/股息率。"""
    try:
        info = data_service.resolve_code(code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析股票代码失败: {e}")

    try:
        detail = data_service.get_stock_detail(info["ts_code"], kind=info["kind"], date=date.strip())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取股票详情失败: {e}")

    # 补充行业 (resolve_code 不返回行业)
    try:
        stocks = data_service._stock_basic()
        hit = stocks[stocks["ts_code"] == info["ts_code"]]
        if not hit.empty:
            info["industry"] = str(hit.iloc[0].get("industry") or "")
    except Exception:
        pass

    return {"info": info, **detail}


@app.get("/api/stock/kline/{code}")
def stock_kline(code: str, freq: str = Query("D", description="周期: D日线/W周线/M月线"),
                adj: str = Query("", description="复权: qfq前复权/hfq后复权/空不复权"),
                start: str = Query("", description="起始日期 YYYYMMDD, 空=20年前"),
                end: str = Query("", description="结束日期 YYYYMMDD, 空=最新")) -> dict:
    """按周期+复权获取 K 线 (供详情页切换日/周/月与复权方式)。"""
    try:
        info = data_service.resolve_code(code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析股票代码失败: {e}")

    try:
        bars = data_service.get_kline(info["ts_code"], kind=info["kind"],
                                      freq=freq, adj=adj, start_date=start, end_date=end)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取 K 线失败: {e}")

    return {"info": info, "freq": freq, "adj": adj, "count": len(bars), "bars": bars}


@app.post("/api/backtest")
def backtest(req: BacktestRequest) -> dict:
    """运行单只股票四策略回测。"""
    try:
        info = data_service.resolve_code(req.ts_code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析股票代码失败: {e}")

    try:
        df = data_service.get_daily(info["ts_code"], kind=info["kind"],
                                    start_date=req.start_date, end_date=req.end_date,
                                    adj="qfq")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取日线数据失败: {e}")

    # 基于首个交易日的收盘价给出价格合理性提示 (不强制拦截)
    first_close = float(df["close"].iloc[0])
    last_close = float(df["close"].iloc[-1])

    try:
        strategies = backtest_engine.run_backtest(
            df,
            capital=req.initial_capital,
            buy_price=req.buy_price,
            sell_price=req.sell_price,
            stop_loss=req.stop_price,
            lookback_days=req.lookback_days,
            gain_threshold=req.gain_threshold,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回测计算失败: {e}")

    return {
        "info": {**info, "ts_code": info["ts_code"]},
        "params": {
            "buy_price": req.buy_price,
            "sell_price": req.sell_price,
            "stop_price": req.stop_price,
            "lookback_days": req.lookback_days,
            "start_date": req.start_date,
            "end_date": df["trade_date"].iloc[-1],
            "initial_capital": req.initial_capital,
        },
        "range": {
            "start": df["trade_date"].iloc[0],
            "end": df["trade_date"].iloc[-1],
            "bars": int(len(df)),
            "first_close": first_close,
            "last_close": last_close,
        },
        "strategies": strategies,
    }


@app.post("/api/fundamental/screen")
def screen_fundamental(req: ScreenRequest) -> dict:
    """基本面选股 (ROE 杜邦拆分): 确保数据入库后, 按行业+多年份查询, 支持筛选与排序。"""
    industry = req.industry.strip()
    try:
        sync_info = fundamental_service.ensure_data(industry, req.years, req.max_stocks)
        items = fundamental_service.screen(industry, req.years, sort_by=req.sort_by,
                                           order=req.order, filters=req.filters, limit=req.limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"基本面选股失败: {e}")
    return {
        "industry": industry,
        "years": req.years,
        "sync": sync_info,
        "count": len(items),
        "items": items,
    }


@app.post("/api/fundamental/init")
def fundamental_init(req: ScreenRequest) -> dict:
    """初始化基本面数据: 按行业(空=全市场)+多个年份计算全部指标并入库 (幂等 upsert)。"""
    try:
        result = fundamental_service.sync_industry_years(
            req.industry.strip(), req.years, max_stocks=req.max_stocks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"基本面数据初始化失败: {e}")
    return result


@app.post("/api/fundamental/verify")
def fundamental_verify(req: ScreenRequest) -> dict:
    """校验 ROE 杜邦拆分正确性: roe ≈ 净利润率 × 总资产周转率 × 权益乘数。"""
    industry = req.industry.strip()
    try:
        items = fundamental_service.screen(industry, req.years, sort_by=req.sort_by,
                                           order=req.order, filters=req.filters, limit=req.limit)
        result = fundamental_service.verify_roe(items)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"校验失败: {e}")
    return result


@app.post("/api/band/optimize")
def band_optimize(req: BandOptimizeRequest) -> dict:
    """区间交易参数自动估算: 搜索最优买入/卖出/止损价, 收益最大化且夏普≥目标。"""
    try:
        info = data_service.resolve_code(req.ts_code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析股票代码失败: {e}")

    try:
        df = data_service.get_daily(info["ts_code"], kind=info["kind"],
                                    start_date=req.start_date, end_date=req.end_date,
                                    adj="qfq")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取日线数据失败: {e}")

    try:
        result = band_service.optimize_band(
            df, capital=req.initial_capital,
            min_sharpe=req.min_sharpe, objective=req.objective,
            max_trades=req.max_trades)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"参数估算失败: {e}")

    return {"info": {**info, "ts_code": info["ts_code"]}, **result}


@app.post("/api/caibao/analyze")
def caibao_analyze(req: CaibaoRequest) -> dict:
    """财报分析: 下载年报 PDF + 提取 + 指标计算 + 生成分析报告 (LLM/规则化)。"""
    try:
        result = caibao_service.analyze(
            req.ts_code, req.start_year, req.end_year, use_llm=req.use_llm)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"财报分析失败: {e}")
    return result


@app.post("/api/dailyrecommend/scan")
def dailyrecommend_scan(req: DailyRecRequest) -> dict:
    """每日推荐扫描: 方法1=区间交易(买入价>=收盘价); 方法2=红利低波(动态股息率>=N, 可加分红率/ROE范围)。"""
    try:
        if req.method == "dividend":
            return daily_recommend_service.recommend_dividend(
                min_dy_ttm=req.min_dy_ttm, industry=req.industry,
                year_min=req.year_min, year_max=req.year_max,
                limit=(req.limit or 500), payout_min=req.payout_min, payout_max=req.payout_max,
                roe_min=req.roe_min, roe_max=req.roe_max)
        result = daily_recommend_service.scan_all(
            codes=req.ts_codes, industry=req.industry, limit=req.limit,
            objective=req.objective, min_sharpe=req.min_sharpe, max_trades=req.max_trades,
            start_date=req.start_date, end_date=req.end_date, use_cache=req.use_cache)
        result["method"] = "band"
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"每日推荐扫描失败: {e}")
    return result


@app.get("/api/dailyrecommend/list")
def dailyrecommend_list(calc_date: str = Query("", description="计算日 YYYYMMDD, 空=最近一天"),
                        industry: str = Query("", description="行业名称(东财分类), 空=全部"),
                        limit: int = Query(500, ge=1, le=2000),
                        method: str = Query("band", description="推荐方法: band/dividend"),
                        min_dy_ttm: float = Query(3.0, ge=0, le=100),
                        year_min: int | None = Query(None, ge=2000, le=2100),
                        year_max: int | None = Query(None, ge=2000, le=2100),
                        payout_min: float | None = Query(None, ge=0, le=1000),
                        payout_max: float | None = Query(None, ge=0, le=1000),
                        roe_min: float | None = Query(None, ge=-100, le=1000),
                        roe_max: float | None = Query(None, ge=-100, le=1000)) -> dict:
    """查询最近推荐 (方法1: buy_price>=close; 方法2: 动态股息率>=N + 分红率/ROE范围 + 年份区间), 可按行业过滤。"""
    try:
        if method == "dividend":
            return daily_recommend_service.recommend_dividend(
                min_dy_ttm=min_dy_ttm, industry=industry,
                year_min=year_min, year_max=year_max, limit=limit,
                payout_min=payout_min, payout_max=payout_max,
                roe_min=roe_min, roe_max=roe_max)
        return daily_recommend_service.get_recommendations(calc_date, limit, industry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取每日推荐失败: {e}")


@app.post("/api/etf/screen")
def etf_screen(req: EtfScreenRequest) -> dict:
    """ETF 筛选: 计算 费用/规模/流动性/折溢价/跟踪偏离/52周 等关键指标并按条件过滤排序。"""
    try:
        return etf_service.screen_etfs(
            keyword=req.keyword.strip(), fund_type=req.fund_type.strip(),
            min_scale=req.min_scale, max_m_fee=req.max_m_fee, max_c_fee=req.max_c_fee,
            min_amount_20=req.min_amount_20, max_premium=req.max_premium,
            min_pos52=req.min_pos52, max_pos52=req.max_pos52,
            sort_by=req.sort_by, order=req.order, limit=req.limit, refresh=req.refresh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ETF 筛选失败: {e}")


@app.post("/api/redlowvol/sync")
def redlowvol_sync(req: RedLowVolRequest) -> dict:
    """按行业+多个年份计算红利低波指标并写入 PostgreSQL (幂等 upsert)。"""
    try:
        result = redlowvol_service.sync_industry_years(
            req.industry.strip(), req.years, max_stocks=req.max_stocks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"红利低波数据同步失败: {e}")
    return result


@app.post("/api/redlowvol/screen")
def redlowvol_screen(req: RedLowVolRequest) -> dict:
    """红利低波选股: 确保数据已入库后, 按行业+多年份查询, 支持阈值筛选与排序。"""
    industry = req.industry.strip()
    try:
        sync_info = redlowvol_service.ensure_data(industry, req.years, max_stocks=req.max_stocks)
        items = redlowvol_service.screen(industry, req.years, sort_by=req.sort_by,
                                         order=req.order, filters=req.filters, limit=req.limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"红利低波选股失败: {e}")
    return {
        "industry": industry,
        "years": req.years,
        "sync": sync_info,
        "count": len(items),
        "items": items,
    }


# ---------------------------------------------------------------------------
# 港股 (红利低波 / 基本面选股) — 数据源: 东财港股财务/分红 + 腾讯日线 + tushare hk_basic
# ---------------------------------------------------------------------------

@app.get("/api/hk/industry/search")
def hk_industry_search(keyword: str = Query("", description="行业关键字, 空=热门行业"),
                       limit: int = Query(20, ge=1, le=50)) -> dict:
    """港股行业模糊搜索: 返回匹配行业及股票数量 (前端候选推荐)。"""
    try:
        ind_map = hk_data_service.industry_map()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    counts: dict[str, int] = {}
    for ind in ind_map.values():
        counts[ind] = counts.get(ind, 0) + 1
    items = [{"industry": k, "count": v} for k, v in counts.items()]
    items.sort(key=lambda x: x["count"], reverse=True)
    if keyword:
        items = [it for it in items if keyword in it["industry"]]
    return {"items": items[:limit]}


@app.get("/api/hk/stocks")
def hk_stocks(keyword: str = Query("", description="代码或名称关键字, 空=全部(前limit只)"),
              limit: int = Query(50, ge=1, le=300)) -> dict:
    """港股股票列表 (代码/名称/行业/市场), 供前端联想。"""
    try:
        stocks = hk_data_service.hk_stock_list()
        ind_map = hk_data_service.industry_map()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    items = []
    for _, row in stocks.iterrows():
        ts_code = str(row["ts_code"])
        name = str(row.get("name") or "")
        market = str(row.get("market") or "")
        ind = ind_map.get(ts_code, "")
        if keyword:
            if keyword not in ts_code and keyword not in name and keyword not in ind:
                continue
        items.append({"ts_code": ts_code, "symbol": ts_code.split(".")[0],
                      "name": name, "market": market, "industry": ind})
    return {"count": len(items), "items": items[:limit]}


@app.post("/api/hk/fundamental/screen")
def hk_screen_fundamental(req: HkScreenRequest) -> dict:
    """港股基本面选股 (ROE 杜邦拆分): 确保数据入库后, 按行业+多年份查询。"""
    industry = req.industry.strip()
    try:
        sync_info = hk_fundamental_service.ensure_data(industry, req.years, req.max_stocks)
        items = hk_fundamental_service.screen(industry, req.years, sort_by=req.sort_by,
                                              order=req.order, filters=req.filters, limit=req.limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"港股基本面选股失败: {e}")
    return {
        "industry": industry,
        "years": req.years,
        "sync": sync_info,
        "count": len(items),
        "items": items,
    }


@app.post("/api/hk/fundamental/init")
def hk_fundamental_init(req: HkScreenRequest) -> dict:
    """初始化港股基本面数据: 按行业(空=全市场)+多个年份计算全部指标并入库 (幂等 upsert)。"""
    try:
        result = hk_fundamental_service.sync_industry_years(
            req.industry.strip(), req.years, max_stocks=req.max_stocks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"港股基本面数据初始化失败: {e}")
    return result


@app.post("/api/hk/fundamental/verify")
def hk_fundamental_verify(req: HkScreenRequest) -> dict:
    """校验港股 ROE 杜邦拆分正确性: roe ≈ 净利润率 × 总资产周转率 × 权益乘数。"""
    industry = req.industry.strip()
    try:
        items = hk_fundamental_service.screen(industry, req.years, sort_by=req.sort_by,
                                              order=req.order, filters=req.filters, limit=req.limit)
        result = hk_fundamental_service.verify_roe(items)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"校验失败: {e}")
    return result


@app.post("/api/hk/redlowvol/sync")
def hk_redlowvol_sync(req: HkRedLowVolRequest) -> dict:
    """港股红利低波: 按行业+多个年份计算指标并写入 PostgreSQL (幂等 upsert)。"""
    try:
        result = hk_redlowvol_service.sync_industry_years(
            req.industry.strip(), req.years, max_stocks=req.max_stocks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"港股红利低波数据同步失败: {e}")
    return result


@app.post("/api/hk/redlowvol/screen")
def hk_redlowvol_screen(req: HkRedLowVolRequest) -> dict:
    """港股红利低波选股: 确保数据已入库后, 按行业+多年份查询, 支持阈值筛选与排序。"""
    industry = req.industry.strip()
    try:
        sync_info = hk_redlowvol_service.ensure_data(industry, req.years, max_stocks=req.max_stocks)
        items = hk_redlowvol_service.screen(industry, req.years, sort_by=req.sort_by,
                                            order=req.order, filters=req.filters, limit=req.limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"港股红利低波选股失败: {e}")
    return {
        "industry": industry,
        "years": req.years,
        "sync": sync_info,
        "count": len(items),
        "items": items,
    }


# ---------------------------------------------------------------------------
# 我的股票 (自选股)
# ---------------------------------------------------------------------------

@app.post("/api/my_stocks/add")
def my_stocks_add(req: MyStockRequest) -> dict:
    """添加自选股 (从股票详情页), 返回是否新增。"""
    try:
        info = data_service.resolve_code(req.ts_code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析股票代码失败: {e}")
    inserted = pg_service.add_my_stock(info["ts_code"], info["name"])
    return {"ok": True, "ts_code": info["ts_code"], "name": info["name"],
            "added": inserted, "already": not inserted}


@app.post("/api/my_stocks/remove")
def my_stocks_remove(req: MyStockRequest) -> dict:
    """移除自选股。"""
    removed = pg_service.remove_my_stock(req.ts_code.strip().upper())
    return {"ok": True, "removed": removed}


@app.get("/api/my_stocks")
def my_stocks_list() -> dict:
    """我的股票列表: 最新收盘/涨跌幅/PE/总市值/股息率/每股分红/52周高低 快照。"""
    stocks = pg_service.list_my_stocks()
    items = []
    for s in stocks:
        try:
            info = data_service.resolve_code(s["ts_code"])
            snap = data_service.get_stock_snapshot(info["ts_code"], kind=info["kind"])
            snap["name"] = info["name"]
            snap["kind"] = info["kind"]
            snap["added_at"] = s["added_at"]
            snap["industry"] = ""
            try:
                hit = data_service._stock_basic()
                hit = hit[hit["ts_code"] == info["ts_code"]]
                if not hit.empty:
                    snap["industry"] = str(hit.iloc[0].get("industry") or "")
            except Exception:
                pass
            items.append(snap)
        except Exception:
            continue
    return {"count": len(items), "items": items}


@app.get("/api/my_stocks/contains/{code}")
def my_stocks_contains(code: str) -> dict:
    """查询某股票是否已在自选股中 (股票详情页按钮初始状态用)。"""
    try:
        info = data_service.resolve_code(code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析股票代码失败: {e}")
    return {"ts_code": info["ts_code"], "in_list": pg_service.has_my_stock(info["ts_code"])}
