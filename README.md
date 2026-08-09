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

在回测结果前，页面会自动展示所查股票/ETF 的**最近行情 K 线图**（近 120 个交易日，
含 MA5/MA20 均线与成交量，支持滚轮缩放与滑块）。K 线图高度自适应浏览器视口
（`clamp(380px, 56vh, 720px)`），窗口缩放时自动重排，方便在 Chrome 中观察；默认显示全部 120 日，输入有效代码后即显示，无需先运行回测。

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
| GET | `/api/quote/{code}?days=120` | 最近 N 交易日行情 (K线) |
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

## 数据库备份与恢复

数据库为本地 PostgreSQL（默认库 `llm_caibao`，连接串从根目录 `.env` 的 `DATABASE_URL` 读取）。

### 导出（全部表结构与数据 → 本地 .sql）

```bash
uv run python scripts/dump_db.py                     # 导出全部 5 张表到 backups/llm_caibao_<时间戳>.sql
uv run python scripts/dump_db.py --out /path/dump.sql # 指定输出路径
uv run python scripts/dump_db.py --table red_low_vol  # 只导出指定表 (可多次)
uv run python scripts/dump_db.py --schema-only        # 只导表结构
uv run python scripts/dump_db.py --data-only          # 只导数据
```

生成 pg_dump plain 风格 SQL：`CREATE TABLE`(列+主键/唯一/检查约束) + 非约束索引 + `COPY` 数据 + 序列 `setval`（外键统一放文件末尾）。自动枚举 `public` schema 全部用户表，无需维护表名单。

### 导入（同机或迁移到其他机器）

```bash
uv run python scripts/restore_db.py --file backups/llm_caibao_20260807_121405.sql
# 其他机器/实例 (--connect 覆盖 DATABASE_URL):
uv run python scripts/restore_db.py --file dump.sql --connect "postgresql://user:pass@host:5432/llm_caibao"
# 保留已有表 (跳过已存在表的 CREATE, 数据仍导入):
uv run python scripts/restore_db.py --file dump.sql --no-drop
```

默认先 `DROP` 再重建 dump 中出现的表（可重复导入）。也可用 PostgreSQL 标准工具导入：

```bash
psql -d llm_caibao -f backups/llm_caibao_20260807_121405.sql
```

> 单表 CSV 备份见 `scripts/export_db.py` / `scripts/import_db.py`（`red_low_vol`、`fundamental_screen`，按业务唯一键 upsert）。

## 港股选股（红利低波 / 基本面）

参考 A 股 `红利低波选股` / `基本面选股` 功能，新增 **港股** 双选股，数据存本地 PostgreSQL
（表 `hk_red_low_vol` / `hk_fundamental_screen`），前端「🇭🇰 港股选股」tab。

### 数据源（本机网络验证）

| 数据 | 来源 | 说明 |
| --- | --- | --- |
| 股票列表 (2782 只) | tushare `hk_basic` | 缓存到 `cache/hk_stock_list.json` |
| 日线 (波动率/年末收盘) | 腾讯港股 K 线 (`web.ifzq.gtimg.cn`) | 不复权, 取最近约 8 年 (count=2000), 避开 tushare `hk_daily` 1次/小时限频 |
| 财务指标 (ROE/毛利率/净利率/负债率/EPS/市值等) | 东财 datacenter `RPT_HKF10_FN_MAININDICATOR` | 年报 (DATE_TYPE_CODE=001) |
| 分红历史 (每股现金分红) | 东财 datacenter `RPT_HKF10_MAIN_DIVBASIC` | 按财政年度聚合多期派息 (如汇丰季度派息) |
| 资产负债表 (流动资产/现金) | 东财 datacenter `RPT_HKF10_FN_BALANCE_PC` | 年报 |
| 行业分类 | 东财 datacenter `RPT_HKF10_INFO_ORGPROFILE` | 批量 ~140 页, 缓存到 `cache/hk_industry.json` |

> 注意: 本机 `push2.eastmoney.com`/`push2his.eastmoney.com` 行情域不可达, 但 `datacenter.eastmoney.com`
> 财务域可达。港股财务/分红/行业均走 datacenter 域。

### 指标口径

- **红利低波**: 静态股息率% = 当年每股分红/年末收盘价；股息率-TTM% = 当年每股分红/最新收盘价；
  波动率% = 日收益 std×√252；分红率% = 每股分红/每股收益；3年股利复合增长%；
  自由现金流 ≈ 经营现金流+投资现金流 (万港元)。金额单位统一为 **万港元**。
- **基本面 (ROE 杜邦拆分)**: ROE ≈ 净利润率 × 总资产周转率 × 权益乘数（归母口径，内部一致）；
  净利率=归母净利/营收、周转=营收/总资产、权益乘数=总资产/归母权益。

### 使用

```bash
# 重建行业映射缓存 (首次或行业变化时)
python scripts/init_hk_redlowvol.py --build-industry

# 指定股票调试 (腾讯/汇丰, 2023~2025)
python scripts/init_hk_redlowvol.py --codes 00700,00005 --years 2023 2024 2025
python scripts/init_hk_fundamental.py --codes 00700,00005 --years 2023 2024 2025

# 按行业初始化 (银行 2023~2025)
python scripts/init_hk_redlowvol.py --industry 银行 --start 2023 --end 2025
python scripts/init_hk_fundamental.py --industry 银行 --start 2023 --end 2025

# 全市场初始化 (一次遍历同时入库两张表, 推荐)
python scripts/init_hk_all_market.py --start 2023 --end 2025          # 串行 (约 46 分钟)
python scripts/init_hk_all_market.py --start 2023 --end 2025 --workers 6   # 6 线程并行 (约 7 分钟)
#   断点续跑: 已同步股票自动跳过 (两表给定年份全部有数据), 中断后重跑即可续
#   其他: --codes 00700,00005 指定股票 / --industry 银行 按行业 / --force 全量重算
#         --batch 50 批量提交 / --limit N 调试 / --sleep 串行间隔
```

API（`ensure_data` 自动补数, 行业模式约 1~3 分钟/年）:

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/hk/redlowvol/screen` | 港股红利低波选股 (自动同步缺失年份) |
| POST | `/api/hk/redlowvol/sync` | 强制重新同步 |
| POST | `/api/hk/fundamental/screen` | 港股基本面选股 (ROE 杜邦) |
| POST | `/api/hk/fundamental/init` | 强制重新同步 |
| POST | `/api/hk/fundamental/verify` | 校验 ROE 杜邦拆分一致性 |
| GET | `/api/hk/industry/search?keyword=银行` | 港股行业联想 (前端候选) |
| GET | `/api/hk/stocks` | 港股列表 (代码/名称/行业/市场) |

请求体与 A 股一致: `{industry, years:[...], sort_by, order, filters:{字段:{min,max}}, max_stocks, limit}`。

### 关键约定 / 坑

- **RMB 柜台股 (`-R`, 如 中国移动-R 80941.HK) 已剔除**: 与主柜同公司重复且无独立财务数据。
  若入库了 `-R` 数据, 可用 `DELETE FROM hk_red_low_vol WHERE name LIKE '%-R'` 清理。
- **`ensure_data` 补数阈值为 0.8×行业股票数**: 部分港股无东财财务数据 (新上市/柜台股),
  避免个别缺数据股票导致每次选股都全量重同步。
- **分红解析**: 从 `PLAN_EXPLAIN` 解析每股现金分红, 优先取「港币X元」折算值 (汇丰等美元派息自动折算);
  排除送股/实物派发 (如派美团/京东股份)。同一财政年度多期派息 (汇丰季度) 自动求和。
- **tushare `hk_daily` 限频 1次/小时**, 日线改用腾讯 K 线; `hk_basic`/`hk_tradecal` 可正常调用。
- 银行等金融机构毛利率/流动比率/存货周转为空属正常 (报表口径不同)。

## 每日定时更新 (crontab)

`scripts/nightly_update.sh` 为每日 20:00 自动更新 A股 + 港股全部选股数据的定时脚本
(已在用户 crontab 安装: `0 20 * * * .../scripts/nightly_update.sh`)。

```bash
# 手动运行一次 (默认只更新最近 1 个完整财年, 如 2025)
bash scripts/nightly_update.sh

# 覆盖年份区间 / 步骤开关
START_YEAR=2023 END_YEAR=2025 bash scripts/nightly_update.sh   # 2023~2025
RUN_HK=0 bash scripts/nightly_update.sh                        # 跳过港股
RUN_A_RECOMMEND=1 bash scripts/nightly_update.sh               # 额外跑 A股每日推荐 (很慢)

# 查看/卸载定时任务
crontab -l
crontab -r   # 注意: 会清空全部 crontab, 建议手动编辑删除对应行
```

自动更新内容与顺序 (各步骤独立, 单个失败不阻塞后续, 日志记录):

| 顺序 | 内容 | 脚本 | 表 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 港股红利低波+基本面 | `init_hk_all_market.py --workers 6 --force` | `hk_red_low_vol` / `hk_fundamental_screen` | 约 7 分钟 |
| 2 | A股红利低波 | `init_redlowvol.py` | `red_low_vol` | 全市场, 较慢 |
| 3 | A股基本面 | `init_fundamental.py` | `fundamental_screen` | 全市场, 较慢 |
| 4 | A股财报 | `init_financial.py` | `financial_data` | 全市场, 较慢 |
| 5 | A股每日推荐 (默认关) | `scan_all_market.py` | `daily_band_recommend` | 设 `RUN_A_RECOMMEND=1` 开启, 很慢 |

- 默认更新年份 = 当前年-1 (最近完整财年), 可用 `START_YEAR`/`END_YEAR` 覆盖。
- 日志写入 `logs/nightly_<时间戳>.log`; 锁文件防止上次未跑完导致本次重叠。
- 说明: 港股用 `--force` 全量刷新 (含最新收盘/股息率TTM); A股脚本为幂等 upsert 全量重算,
  全市场耗时较长, 若 tushare 限频导致某步骤失败, 脚本会记录失败并继续, 次日自动重跑。

## 目录结构

```
api/
  main.py               # FastAPI 入口 + 路由 (含港股 /api/hk/*)
  backtest_engine.py    # 三策略逻辑 + 指标计算
  data_service.py       # A 股 tushare 数据获取 / 代码解析 / 缓存
  hk_data_service.py    # 港股数据层 (tushare hk_basic + 东财 datacenter + 腾讯K线)
  hk_redlowvol_service.py  # 港股红利低波选股
  hk_fundamental_service.py# 港股基本面选股 (ROE 杜邦拆分)
  redlowvol_service.py  # A 股红利低波选股
  fundamental_service.py# A 股基本面选股
  pg_service.py         # PostgreSQL 存取 (含 hk_red_low_vol / hk_fundamental_screen)
  static/               # 前端页面 (index.html / css / js)
  requirements.txt
scripts/
  nightly_update.sh     # 每日 20:00 定时更新 A股+港股 (crontab)
  init_hk_all_market.py # 港股全市场初始化 (红利低波+基本面一次遍历, 支持并行/断点续跑)
  init_hk_redlowvol.py  # 港股红利低波数据初始化
  init_hk_fundamental.py# 港股基本面数据初始化
  dump_db.py            # 导出全部表结构与数据到 .sql
  restore_db.py         # 从 .sql 导入 (支持迁移到其他机器)
  export_db.py          # 导出业务表到 CSV 备份
  import_db.py          # 从 CSV 备份导入 (幂等 upsert)
cache/                  # 港股股票列表/行业映射缓存 (可重新生成, gitignore)
```

> 仅供研究参考，不构成投资建议。
