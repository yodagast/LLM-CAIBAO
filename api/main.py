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

from . import backtest_engine, data_service
from . import fundamental_service, pg_service, redlowvol_service

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
    max_stocks: int = Field(500, ge=1, le=500, description="同步时最多扫描股票数(全市场时生效)")
    limit: int = Field(1000, ge=1, le=2000, description="返回数量上限")


class RedLowVolRequest(BaseModel):
    """红利低波选股请求。"""
    industry: str = Field("", description="行业名称(东财分类), 空表示全市场")
    years: list[int] = Field(..., min_length=1, max_length=15,
                             description="年度列表, 如 [2020,2021,2022,2023,2024,2025]")
    sort_by: str = Field("dividend_yield", description="排序字段 (股息率/波动率/每股分红/自由现金流等)")
    order: str = Field("desc", description="排序方向: asc / desc")
    filters: dict = Field(default_factory=dict,
                          description="筛选条件 {字段: {min: x, max: y}}, 如 {'dividend_yield': {'min': 5}}")
    max_stocks: int = Field(500, ge=1, le=500, description="同步时最多扫描股票数")
    limit: int = Field(500, ge=1, le=1000, description="返回数量上限")


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
                                    start_date=req.start_date, end_date=req.end_date)
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
