# LLM-CAIBAO 单股三策略回测系统

基于 **tushare** 数据源 + **FastAPI** 后端的单只股票回测网站。参考 `strategy/backtest_baijiu_3in1.py`
的三合一策略思路，对单只股票独立运行三种策略并对比：

| 策略 | 逻辑 |
| --- | --- |
| 买入持有 | 首日以全部资金买入，一直持有 |
| 区间交易 | 收盘 ≤ 买入价 买入全部；收盘 ≥ 卖出价 或 ≤ 止损价 卖出全部 |
| 低价买入 | 收盘价为 N 日最低 且 ≤ 买入价 时买入；达卖出价 / 涨幅超阈值 / 破止损 时卖出 |

## 快速开始

```bash
cd LLM-CAIBAO
uv sync                 # 安装依赖 (fastapi/uvicorn/tushare 等)
uv run uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 http://127.0.0.1:8000 使用回测页面。

> tushare token 从项目根目录 `.env` 读取（`TUSHARE_TOKEN=...`）。

## 使用说明

输入：股票 / **ETF** 代码（支持 6 位代码或带后缀，如 `000858` / `600036.SH`；ETF 如 `513050`、`588000`、
`510300` 亦支持，系统自动识别并走 `fund_daily` 数据源）、**买入价、卖出价、止损价**，
以及可选参数（低价买入 N 日窗口、初始资金、起始日期、涨幅阈值）。

输出：三种策略的**累计收益曲线**与指标表（总收益率 / 年化收益率 / **最大回撤** / **卡玛比率** / **夏普比率** / 最终资产）。

### ETF 回测

- 支持沪深两市 ETF/LOF：沪市 `51/56/58/50` 开头，深市 `15/16/18` 开头。
- 输入 6 位 ETF 代码（如 `513050`）或带后缀（如 `513050.SH`）均可。
- 前端输入框支持股票与 ETF 混合联想搜索，解析后会标注「股票 / 基金/ETF」。
- ETF 日线数据来自 tushare `fund_daily` 接口，回测逻辑与股票完全一致。

## API 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/stock/search?keyword=五粮` | 股票/基金联想搜索 |
| GET | `/api/stock/{code}` | 解析股票代码 |
| POST | `/api/backtest` | 运行三策略回测 |
| GET | `/` | 回测页面 |

### POST /api/backtest 请求示例

```json
{
  "ts_code": "000858.SZ",
  "buy_price": 75,
  "sell_price": 150,
  "stop_price": 60,
  "lookback_days": 20,
  "start_date": "20170101",
  "end_date": "",
  "initial_capital": 100000,
  "gain_threshold": 20
}
```

响应包含 `info`（股票信息）、`params`、`range`（数据区间）与 `strategies`
（每个策略的 `dates`、`values`、`returns_pct` 与 `metrics`：`total_return`、`annual_return`、
`max_drawdown`、`calmar`、`sharpe`、`final_value`）。

## 目录结构

```
api/
  main.py             # FastAPI 入口 + 路由
  backtest_engine.py  # 三策略逻辑 + 指标计算
  data_service.py     # tushare 数据获取 / 代码解析 / 缓存
  static/             # 前端页面 (index.html / css / js)
  requirements.txt
```

> 仅供研究参考，不构成投资建议。
