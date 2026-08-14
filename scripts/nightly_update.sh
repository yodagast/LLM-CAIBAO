#!/bin/bash
# ============================================================================
# 每日 20:00 定时更新 A股 + 港股全部选股数据
#
# 安装定时任务 (crontab, macOS 语法, 每天 20:00):
#   0 20 * * * /Users/huangyong/git/LLM-CAIBAO/scripts/nightly_update.sh
#
# 自动更新内容:
#   1) 港股 红利低波 + 基本面  (init_hk_all_market.py, --force 全量刷新, --workers 6 约 7 分钟)
#   2) A股  红利低波          (init_redlowvol.py  → red_low_vol)
#   3) A股  基本面            (init_fundamental.py → fundamental_screen)
#   4) A股  财报              (init_financial.py   → financial_data)
#   5) ETF  筛选数据          (init_etf.py         → etf_screen)
#   6) A股  选股新字段回填    (backfill_margin_fcf.py → 补齐历史年份 毛利率/自由现金流, 全市场)
#   7) 本地 日线+财务持久化   (sync_local_bars.py → 我的股票/策略Hub股票/ETF 最近10年日线+财务, 前端优先读库)
#   8) A股  每日推荐          (scan_all_market.py, 默认关闭; 全市场约数小时, 设 RUN_A_RECOMMEND=1 开启)
#   9) 公司大事               (sync_stock_events.py → 网络搜索+DeepSeek总结, 前端详情页时间线)
#
# 默认更新最近 1 个完整财年 (当前年-1, 如 2025); 可用环境变量覆盖:
#   START_YEAR / END_YEAR   年份区间 (如 START_YEAR=2023 END_YEAR=2025)
#   RUN_HK / RUN_A_RLV / RUN_A_FUND / RUN_A_FIN / RUN_A_ETF / RUN_A_BACKFILL / RUN_A_BARS / RUN_A_RECOMMEND / RUN_EVENTS  各步骤开关 (0=关 1=开)
#
# 日志写入 logs/nightly_<时间戳>.log; 锁文件防止上次未跑完导致本次重叠。
# ============================================================================
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJECT_ROOT/.venv/bin/python"
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"

# 防止重叠运行 (若上次仍在跑, 直接跳过本次)
LOCK_FILE="$LOG_DIR/nightly.lock"
if [ -f "$LOCK_FILE" ] && kill -0 "$(cat "$LOCK_FILE" 2>/dev/null)" 2>/dev/null; then
  echo "$(date '+%F %T') 上次运行仍在进行, 跳过本次。"
  exit 0
fi
echo "$$" > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# 本次日志文件 (全部输出重定向到日志, 同时保留控制台)
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$LOG_DIR/nightly_${STAMP}.log"
exec >>"$LOG_FILE" 2>&1

# 更新年份: 默认最近 1 个完整财年 (当前年-1; macOS date -v 语法)
END_YEAR="${END_YEAR:-$(date -v-1y +%Y)}"
START_YEAR="${START_YEAR:-$END_YEAR}"

# 各步骤开关 (默认全开; 每日推荐默认关)
RUN_HK="${RUN_HK:-1}"
RUN_A_RLV="${RUN_A_RLV:-1}"
RUN_A_FUND="${RUN_A_FUND:-1}"
RUN_A_FIN="${RUN_A_FIN:-1}"
RUN_A_ETF="${RUN_A_ETF:-1}"
RUN_A_BACKFILL="${RUN_A_BACKFILL:-1}"
RUN_A_BARS="${RUN_A_BARS:-1}"
RUN_A_RECOMMEND="${RUN_A_RECOMMEND:-0}"
RUN_EVENTS="${RUN_EVENTS:-1}"

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
log "项目: $PROJECT_ROOT | Python: $VENV_PY"
log "年份区间: $START_YEAR ~ $END_YEAR"
cd "$PROJECT_ROOT" || exit 1

FAILED=0

# 1) 港股全市场 (红利低波 + 基本面, 一次遍历), --force 全量刷新 (约 7 分钟)
if [ "$RUN_HK" = "1" ]; then
  run_step "港股 红利低波+基本面 (全市场)" \
    "$VENV_PY" scripts/init_hk_all_market.py --start "$START_YEAR" --end "$END_YEAR" --workers 6 --force
fi

# 2) A股 红利低波 (全市场, 幂等 upsert)
if [ "$RUN_A_RLV" = "1" ]; then
  run_step "A股 红利低波 (全市场)" \
    "$VENV_PY" scripts/init_redlowvol.py --start "$START_YEAR" --end "$END_YEAR"
fi

# 3) A股 基本面 (全市场, 幂等 upsert)
if [ "$RUN_A_FUND" = "1" ]; then
  run_step "A股 基本面 (全市场)" \
    "$VENV_PY" scripts/init_fundamental.py --start "$START_YEAR" --end "$END_YEAR"
fi

# 4) A股 财报 financial_data (财报挖掘 tab 数据)
if [ "$RUN_A_FIN" = "1" ]; then
  run_step "A股 财报 financial_data" \
    "$VENV_PY" scripts/init_financial.py --start "$START_YEAR" --end "$END_YEAR"
fi

# 5) ETF 筛选数据 etf_screen (ETF 筛选 tab 数据, 幂等 upsert, 约 20~60 分钟)
if [ "$RUN_A_ETF" = "1" ]; then
  run_step "ETF 筛选数据 (全市场)" \
    "$VENV_PY" scripts/init_etf.py --batch 200
fi

# 6) A股 选股新字段回填 (补齐历史年份 毛利率/自由现金流, 仅回填 NULL 行, 可重复续跑)
if [ "$RUN_A_BACKFILL" = "1" ]; then
  run_step "A股 选股新字段回填 (毛利率/自由现金流, 全市场)" \
    "$VENV_PY" scripts/backfill_margin_fcf.py
fi

# 7) 本地 日线+财务持久化 (我的股票/策略Hub股票/ETF 最近10年日线 + 财务, 前端优先从 pgsql 加载)
if [ "$RUN_A_BARS" = "1" ]; then
  run_step "本地 日线+财务持久化 (目标列表)" \
    "$VENV_PY" scripts/sync_local_bars.py
fi

# 8) A股 每日推荐 (可选, 默认关闭; 全市场估算区间交易参数较慢, 支持断点续跑)
if [ "$RUN_A_RECOMMEND" = "1" ]; then
  run_step "A股 每日推荐 (全市场)" \
    "$VENV_PY" scripts/scan_all_market.py
fi

# 9) 公司大事 (网络搜索 + DeepSeek 总结; 只补缺失, 不重复生成)
if [ "$RUN_EVENTS" = "1" ]; then
  run_step "公司大事同步 (目标列表)" \
    "$VENV_PY" scripts/sync_stock_events.py
fi

if [ "$FAILED" = "0" ]; then
  log "==================== 每日数据更新完成 (全部成功) ===================="
  exit 0
else
  log "==================== 每日数据更新结束 (存在失败步骤, 详见上方) ===================="
  exit 1
fi
