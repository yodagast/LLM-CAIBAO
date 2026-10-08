#!/bin/bash
# ============================================================================
# 每日 20:00 定时更新 A股 + 港股全部选股数据 (Linux 版本)
#
# 适用环境: Linux (Alibaba Cloud Linux / RHEL8 等, GNU coreutils), 兼容 macOS。
# 与 macOS 版 nightly_update.sh 差异:
#   - 年份计算用 GNU date (自动兼容 BSD date)
#   - 项目根目录 / Python 路径可配置 (Linux 部署路径/venv 可能与开发机不同)
#
# 在 Linux 服务器安装定时任务 (crontab, 每天 20:00):
#   crontab -e  加入:
#   0 20 * * * /path/to/LLM-CAIBAO/scripts/nightly_update_linux.sh >/dev/null 2>&1
#
# 自动更新内容:
#   0) 自选股/策略Hub 日线增量同步  (sync_target_daily.py → 前端读库的行情, **最高优先级**)
#   1) 港股 红利低波 + 基本面  (init_hk_all_market.py, --force 全量刷新, --workers 6)
#   2) A股  红利低波          (init_redlowvol.py  → red_low_vol)
#   3) A股  基本面            (init_fundamental.py → fundamental_screen)
#   4) A股  财报              (init_financial.py   → financial_data)
#   5) ETF  筛选数据          (init_etf.py         → etf_screen)
#   6) A股  选股新字段回填    (backfill_margin_fcf.py → 补齐历史年份 毛利率/自由现金流, 全市场)
#   8) 本地 日线+财务持久化   (sync_local_bars.py → 我的股票/策略Hub股票/ETF 日线增量为主+财务, A股+港股, 前端优先读库; 财务按本地已有年份增量补齐)
#   9) A股  低价选股          (sync_low_price.py → 全市场扫描接近52周低点公司, 入库 low_price_screen, 前端优先读库)
#   10) 港股  低价选股          (sync_hk_low_price.py → 全市场扫描接近52周低点港股, 入库 hk_low_price_screen, 前端优先读库)
#   11) A股  每日推荐          (不更新: 默认关闭 RUN_A_RECOMMEND=0, 不执行 scan_all_market.py)
#   12) 公司大事              (sync_stock_events.py → 网络搜索+DeepSeek总结; 不足20件增量更新, 达到20件仅月度更新)
#
# **每步独立超时 (STEP_TIMEOUT, 默认 1800s)**: 某步卡死(如线上 DNS 故障时全市场扫描
# 速率暴跌)会被强制终止并继续后续步骤, 避免"前面的步骤卡死导致后面步骤整晚没跑到"。
#
# 默认更新最近 1 个完整财年 (当前年-1); 可用环境变量覆盖:
#   PROJECT_ROOT  项目根目录 (默认: 本脚本所在目录的上级)
#   PYTHON_BIN    解释器路径 (默认: $PROJECT_ROOT/.venv/bin/python, 可按部署环境指定)
#   START_YEAR / END_YEAR   年份区间
#   LOOKBACK_DAYS           日线增量回看自然日数 (默认 30, 覆盖长假)
#   STEP_TIMEOUT            单步超时秒数 (默认 1800; 全市场步骤可按需调大, 如 7200)
#   RUN_HK / RUN_A_RLV / RUN_A_FUND / RUN_A_FIN / RUN_A_ETF / RUN_A_BACKFILL / RUN_TARGET_BARS / RUN_A_BARS / RUN_A_LOW / RUN_HK_LOW / RUN_A_RECOMMEND / RUN_EVENTS  各步骤开关 (0=关 1=开)
#
# 排查前端数据不新鲜: `python scripts/check_daily_data.py` (逐层体检, --fix 可补数)
#
# 日志写入 $PROJECT_ROOT/logs/nightly_<时间戳>.log; 锁文件防止上次未跑完导致本次重叠。
# ============================================================================
set -uo pipefail

# 项目根目录 / Python (均可通过环境变量覆盖, 适配不同部署路径)
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"

# cron 环境 PATH 较精简, 补齐常用路径 (date/mkdir/cat/kill 等)
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

# 校验 Python 存在
if [ ! -x "$PYTHON_BIN" ]; then
  echo "$(date '+%F %T') 未找到 Python: $PYTHON_BIN (可用环境变量 PYTHON_BIN 指定)" >&2
  exit 1
fi

# 防止重叠运行 (若上次仍在跑, 直接跳过本次)
# 锁文件格式: "<pid> <epoch秒>"; 仅当 PID 存活**且**锁未陈旧时才跳过。
# 只存 PID 会在 PID 被复用时误判"上次仍在跑"而静默跳过整晚任务 (数据不更新)。
LOCK_FILE="$LOG_DIR/nightly.lock"
LOCK_STALE_SECS="${LOCK_STALE_SECS:-43200}"   # 12h 视为陈旧
if [ -f "$LOCK_FILE" ]; then
  read -r LOCK_PID LOCK_TS _ < "$LOCK_FILE" 2>/dev/null || LOCK_PID=""; LOCK_TS=""
  NOW_TS="$(date +%s)"
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null \
     && [ -n "$LOCK_TS" ] && [ "$((NOW_TS - LOCK_TS))" -lt "$LOCK_STALE_SECS" ]; then
    echo "$(date '+%F %T') 上次运行仍在进行 (pid=$LOCK_PID), 跳过本次。" >&2
    exit 0
  fi
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    echo "$(date '+%F %T') 发现陈旧锁 (pid=$LOCK_PID 存活但锁已超 ${LOCK_STALE_SECS}s), 接管本次运行。" >&2
  fi
fi
echo "$$ $(date +%s)" > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# 本次日志文件 (全部输出重定向到日志)
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$LOG_DIR/nightly_${STAMP}.log"
exec >>"$LOG_FILE" 2>&1

# 更新年份: 默认最近 1 个完整财年 (当前年-1); 兼容 GNU (Linux) 与 BSD (macOS) date
if date -d "-1 year" +%Y >/dev/null 2>&1; then
  END_YEAR="${END_YEAR:-$(date -d "-1 year" +%Y)}"
else
  END_YEAR="${END_YEAR:-$(date -v-1y +%Y)}"
fi
START_YEAR="${START_YEAR:-$END_YEAR}"

# 各步骤开关 (默认全开; 每日推荐默认关)
RUN_HK="${RUN_HK:-1}"
RUN_A_RLV="${RUN_A_RLV:-1}"
RUN_A_FUND="${RUN_A_FUND:-1}"
RUN_A_FIN="${RUN_A_FIN:-1}"
RUN_A_ETF="${RUN_A_ETF:-1}"
RUN_A_BACKFILL="${RUN_A_BACKFILL:-1}"
RUN_TARGET_BARS="${RUN_TARGET_BARS:-1}"
RUN_A_BARS="${RUN_A_BARS:-1}"
RUN_A_LOW="${RUN_A_LOW:-1}"
RUN_HK_LOW="${RUN_HK_LOW:-1}"
RUN_A_RECOMMEND="${RUN_A_RECOMMEND:-0}"
RUN_EVENTS="${RUN_EVENTS:-1}"

log() { echo "[$(date '+%F %T')] $*"; }

# 单步超时 (秒, 可用 STEP_TIMEOUT 覆盖)。**防止某步卡死导致后续步骤永不执行**:
# 曾出现线上 DNS 故障时第 1 步(港股全市场)速率跌到 0.1只/s、预计需 46 小时, 于是
# 后面的日线同步步骤整晚都没跑到, 前端数据冻结在旧日期。
STEP_TIMEOUT="${STEP_TIMEOUT:-1800}"

run_step() {
  local timeout="$STEP_TIMEOUT"
  # 可选第 1 参数为数字时作为本步超时 (秒)
  if [ "${1:-}" != "" ] && [ "$1" -eq "$1" ] 2>/dev/null; then
    timeout="$1"; shift
  fi
  local label="$1"; shift
  log ">>> [$label] 开始 (超时 ${timeout}s)"
  "$@" &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$timeout" ]; then
      log "!!! [$label] 超时 (${timeout}s), 终止该步骤并继续后续步骤"
      kill -TERM "$pid" 2>/dev/null || true
      sleep 3
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      FAILED=1
      return
    fi
    sleep 5
    waited=$((waited + 5))
  done
  wait "$pid"
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    log ">>> [$label] 完成"
  else
    log "!!! [$label] 失败 (exit $rc)"
    FAILED=1
  fi
}

log "==================== 每日数据更新开始 ===================="
log "项目: $PROJECT_ROOT | Python: $PYTHON_BIN"
log "年份区间: $START_YEAR ~ $END_YEAR"
log "单步超时: ${STEP_TIMEOUT}s"
cd "$PROJECT_ROOT" || exit 1

FAILED=0

# 0) 【最高优先级】自选股/策略Hub 日线增量同步 (A股/ETF + 港股)
#    前端详情页/自选股「最近收盘」优先读 stock_daily_bars —— 这一步决定前端看到
#    的数据是否新鲜。放在最前面, 确保即使后面的全市场重算步骤失败/超时, 也不会
#    出现"日线冻结在旧日期"的问题 (历史上曾因第 1 步港股全市场卡死而整晚没跑到)。
if [ "$RUN_TARGET_BARS" = "1" ]; then
  run_step "自选股/策略Hub 日线增量同步 (A股+港股)" \
    "$PYTHON_BIN" scripts/sync_target_daily.py --lookback-days "${LOOKBACK_DAYS:-30}"
fi

# 1) 港股全市场 (红利低波 + 基本面, 一次遍历), --force 全量刷新
if [ "$RUN_HK" = "1" ]; then
  run_step "港股 红利低波+基本面 (全市场)" \
    "$PYTHON_BIN" scripts/init_hk_all_market.py --start "$START_YEAR" --end "$END_YEAR" --workers 6 --force
fi

# 2) A股 红利低波 (全市场, 幂等 upsert)
if [ "$RUN_A_RLV" = "1" ]; then
  run_step "A股 红利低波 (全市场)" \
    "$PYTHON_BIN" scripts/init_redlowvol.py --start "$START_YEAR" --end "$END_YEAR"
fi

# 3) A股 基本面 (全市场, 幂等 upsert)
if [ "$RUN_A_FUND" = "1" ]; then
  run_step "A股 基本面 (全市场)" \
    "$PYTHON_BIN" scripts/init_fundamental.py --start "$START_YEAR" --end "$END_YEAR"
fi

# 4) A股 财报 financial_data (财报挖掘 tab 数据)
if [ "$RUN_A_FIN" = "1" ]; then
  run_step "A股 财报 financial_data" \
    "$PYTHON_BIN" scripts/init_financial.py --start "$START_YEAR" --end "$END_YEAR"
fi

# 5) ETF 筛选数据 etf_screen (ETF 筛选 tab 数据, 幂等 upsert, 约 20~60 分钟)
if [ "$RUN_A_ETF" = "1" ]; then
  run_step "ETF 筛选数据 (全市场)" \
    "$PYTHON_BIN" scripts/init_etf.py --batch 200
fi

# 6) A股 选股新字段回填 (补齐历史年份 毛利率/自由现金流, 仅回填 NULL 行, 可重复续跑)
if [ "$RUN_A_BACKFILL" = "1" ]; then
  run_step "A股 选股新字段回填 (毛利率/自由现金流, 全市场)" \
    "$PYTHON_BIN" scripts/backfill_margin_fcf.py
fi

# 8) 本地 日线+财务持久化 (目标列表日线增量 + 财务; 本地无数据的股票自动全量首次入库)
if [ "$RUN_A_BARS" = "1" ]; then
  run_step "本地 日线(A股+港股)+财务持久化 (目标列表)" \
    "$PYTHON_BIN" scripts/sync_local_bars.py --lookback-days "${LOOKBACK_DAYS:-30}"
fi

# 9) A股 低价选股 (全市场扫描接近52周低点公司, 入库 low_price_screen, 前端优先读库)
if [ "$RUN_A_LOW" = "1" ]; then
  run_step "A股 低价选股 (全市场, 接近52周低点)" \
    "$PYTHON_BIN" scripts/sync_low_price.py
fi

# 10) 港股 低价选股 (全市场扫描接近52周低点港股, 入库 hk_low_price_screen, 前端优先读库)
if [ "$RUN_HK_LOW" = "1" ]; then
  run_step "港股 低价选股 (全市场, 接近52周低点)" \
    "$PYTHON_BIN" scripts/sync_hk_low_price.py
fi

# 11) A股 每日推荐 (可选, 默认关闭; 全市场估算区间交易参数较慢, 支持断点续跑)
if [ "$RUN_A_RECOMMEND" = "1" ]; then
  run_step "A股 每日推荐 (全市场)" \
    "$PYTHON_BIN" scripts/scan_all_market.py
fi

# 12) 公司大事 (网络搜索 + DeepSeek 总结; 只补缺失, 不重复生成)
if [ "$RUN_EVENTS" = "1" ]; then
  run_step "公司大事同步 (目标列表)" \
    "$PYTHON_BIN" scripts/sync_stock_events.py
fi

if [ "$FAILED" = "0" ]; then
  log "==================== 每日数据更新完成 (全部成功) ===================="
  exit 0
else
  log "==================== 每日数据更新结束 (存在失败步骤, 详见上方) ===================="
  exit 1
fi
