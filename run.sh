#!/bin/bash
# ============================================================================
# FastAPI 服务 启动 / 重启 / 停止 管理脚本
#
# 用法:
#   ./run.sh start          启动服务 (后台运行, 日志 logs/uvicorn.log)
#   ./run.sh stop           停止服务 (优雅 TERM 信号)
#   ./run.sh restart        重启服务 (先 stop 再 start)
#   ./run.sh status         查看服务运行状态
#   ./run.sh                (无参数 = status)
#
# 覆盖项 (环境变量):
#   HOST=0.0.0.0 PORT=9000 WORKERS=2 ./run.sh start   自定义监听与 worker 数
#   INFINITE=1 ./run.sh start                         不写 pid 文件 (运行方式取反)
#
# 依赖: 项目根目录 .env (环境变量), .venv (虚拟环境)。
# ============================================================================
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_UVICORN="$PROJECT_ROOT/.venv/bin/uvicorn"
LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/uvicorn.log"
PID_FILE="$LOG_DIR/uvicorn.pid"
ENV_FILE="$PROJECT_ROOT/.env"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"

log() { echo "[$(date '+%F %T')] $*"; }

# 读取 .env (仅注入未设置的环境变量, 不影响用户已有环境)
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%[[:space:]]}"
    case "$line" in
      ""|"#"*) continue ;;
    esac
    key="${line%%=*}"; value="${line#*=}"
    [ -n "$key" ] && export "$key=$value" 2>/dev/null
  done < "$ENV_FILE"
fi

if [ ! -x "$VENV_UVICORN" ]; then
  echo "错误: 未找到 $VENV_UVICORN, 请先创建虚拟环境 (.venv) 并安装依赖。" >&2
  exit 1
fi

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null
}

get_pid() { cat "$PID_FILE" 2>/dev/null; }

start() {
  if is_running; then
    log "服务已在运行 (pid=$(get_pid), ${HOST}:${PORT}), 无需重复启动。"
    return 0
  fi
  mkdir -p "$LOG_DIR"
  log "启动 FastAPI 服务: ${HOST}:${PORT} (workers=$WORKERS)"
  if [ "${INFINITE:-0}" = "1" ]; then
    # 前台运行 (调试用), 不写 pid
    exec "$VENV_UVICORN" api.main:app --host "$HOST" --port "$PORT"
    return
  fi
  # 后台运行并把 pid 写入日志目录
  nohup "$VENV_UVICORN" api.main:app --host "$HOST" --port "$PORT" \
      --workers "$WORKERS" >"$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  # 等待健康检查通过 (最多 60 秒)
  for i in $(seq 1 60); do
    if curl -fsS "http://${HOST}:${PORT}/api/health" >/dev/null 2>&1; then
      log "启动完成 (pid=$(get_pid)), 健康检查通过: http://${HOST}:${PORT}/api/health"
      return 0
    fi
    sleep 1
  done
  log "警告: 健康检查超时, 服务可能未就绪, 请查看 $LOG_FILE"
  return 1
}

stop() {
  if ! is_running; then
    log "服务未在运行。"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid; pid="$(get_pid)"
  log "停止服务 (pid=$pid)..."
  kill "$pid" 2>/dev/null
  # 等待进程退出 (最多 20 秒)
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    log "进程未响应 TERM, 强制结束 (pid=$pid)..."
    kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$PID_FILE"
  log "已停止。"
}

status() {
  if is_running; then
    log "运行中 (pid=$(get_pid), ${HOST}:${PORT})"
  else
    log "未运行。"
    return 1
  fi
}

restart() {
  stop
  start
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) restart ;;
  status)  status ;;
  *) echo "用法: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac