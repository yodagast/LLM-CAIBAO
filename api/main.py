"""单只股票三策略回测 Web 系统 (FastAPI)。

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

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="单股三策略回测系统",
    description="基于 tushare 数据源, 对单只股票运行 买入持有 / 区间交易 / 低价买入 三种策略回测。",
    version="1.0.0",
)

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


@app.post("/api/backtest")
def backtest(req: BacktestRequest) -> dict:
    """运行单只股票三策略回测。"""
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
