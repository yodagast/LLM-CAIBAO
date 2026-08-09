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
#   1) 港股 红利低波 + 基本面  (init_hk_all_market.py, --force 全量刷新, --workers 6)
#   2) A股  红利低波          (init_redlowvol.py  → red_low_vol)
#   3) A股  基本面            (init_fundamental.py → fundamental_screen)
#   4) A股  财报              (init_financial.py   → financial_data)
#   5) A股  每日推荐          (scan_all_market.py, 默认关闭; 全市场较慢, 设 RUN_A_RECOMMEND=1 开启)
#
# 默认更新最近 1 个完整财年 (当前年-1); 可用环境变量覆盖:
#   PROJECT_ROOT  项目根目录 (默认: 本脚本所在目录的上级)
#   PYTHON_BIN    解释器路径 (默认: $PROJECT_ROOT/.venv/bin/python, 可按部署环境指定)
#   START_YEAR / END_YEAR   年份区间
#   RUN_HK / RUN_A_RLV / RUN_A_FUND / RUN_A_FIN / RUN_A_RECOMMEND  各步骤开关 (0=关 1=开)
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
LOCK_FILE="$LOG_DIR/nightly.lock"
if [ -f "$LOCK_FILE" ] && kill -0 "$(cat "$LOCK_FILE" 2>/dev/null)" 2>/dev/null; then
  echo "$(date '+%F %T') 上次运行仍在进行, 跳过本次。" >&2
  exit 0
fi
echo "$$" > "$LOCK_FILE"
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
RUN_A_RECOMMEND="${RUN_A_RECOMMEND:-0}"

log() { echo "[$(date '+%F %T')] $*"; }

run_step() {
  local label="$1"; shift
  log ">>> [$label] 开始"
  "$@"
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
cd "$PROJECT_ROOT" || exit 1

FAILED=0

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

# 5) A股 每日推荐 (可选, 默认关闭; 全市场估算区间交易参数较慢, 支持断点续跑)
if [ "$RUN_A_RECOMMEND" = "1" ]; then
  run_step "A股 每日推荐 (全市场)" \
    "$PYTHON_BIN" scripts/scan_all_market.py
fi

if [ "$FAILED" = "0" ]; then
  log "==================== 每日数据更新完成 (全部成功) ===================="
  exit 0
else
  log "==================== 每日数据更新结束 (存在失败步骤, 详见上方) ===================="
  exit 1
fi
