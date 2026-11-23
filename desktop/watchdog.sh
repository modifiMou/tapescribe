#!/bin/zsh
# 心跳看门狗：由 launchd 每 30 秒调用一次。进程活着但卡死时 KeepAlive 不会重启，这里补上。
set -uo pipefail

LABEL="com.kbrain.asrworkbench"
PORT="${ASRWB_PORT:-8770}"
DIR="$HOME/Library/Application Support/asrWorkbench"
FAILS="$DIR/watchdog.fails"
LOG="$HOME/Library/Logs/asrWorkbench.log"
LIMIT=6

mkdir -p "$DIR"

# 说话人分离跑在子进程里，7 小时级录音要算几十分钟。子进程仍在吃 CPU 就说明任务在正常推进，
# 心跳慢不是卡死，此时重启只会白白丢掉进度。
worker_busy() {
  local pid cpu
  for pid in ${(f)"$(/usr/bin/pgrep -f 'app\.diarize_worker' 2>/dev/null)"}; do
    cpu=$(/bin/ps -o %cpu= -p "$pid" 2>/dev/null | /usr/bin/tr -d ' ')
    [[ -n "$cpu" ]] || continue
    (( ${cpu%.*} >= 20 )) && return 0
  done
  return 1
}

if /usr/bin/curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/state" 2>/dev/null; then
  rm -f "$FAILS"
  exit 0
fi

if worker_busy; then
  echo "[watchdog $(/bin/date '+%F %T')] 说话人分离子进程仍在计算，跳过重启" >> "$LOG"
  rm -f "$FAILS"
  exit 0
fi

count=$(cat "$FAILS" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$FAILS"
echo "[watchdog $(/bin/date '+%F %T')] 心跳失败 $count/$LIMIT" >> "$LOG"

(( count < LIMIT )) && exit 0

# 收尾复核：慢一点再问一次，避免把「一时很忙」当成卡死
if /usr/bin/curl -fsS -m 10 -o /dev/null "http://127.0.0.1:$PORT/api/state" 2>/dev/null; then
  echo "[watchdog $(/bin/date '+%F %T')] 复核已恢复响应，先不重启" >> "$LOG"
  rm -f "$FAILS"
  exit 0
fi

echo "[watchdog $(/bin/date '+%F %T')] 连续 $LIMIT 次无响应，强制重启服务" >> "$LOG"
/usr/bin/pkill -9 -f "python3.12 -m app.main" >/dev/null 2>&1
/usr/bin/pkill -9 -f "/.venv/bin/python -m app.main" >/dev/null 2>&1
# 主进程被强杀后子进程会变成孤儿继续占 CPU，一起收掉
/usr/bin/pkill -9 -f "app.diarize_worker" >/dev/null 2>&1
/bin/sleep 1
/usr/bin/launchctl kickstart -k "gui/$(/usr/bin/id -u)/$LABEL" >/dev/null 2>&1
rm -f "$FAILS"
exit 0
