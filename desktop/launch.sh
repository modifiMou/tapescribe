#!/bin/zsh
# 桌面快捷方式调用的启动脚本：已运行则直接打开页面；未运行则拉起服务（优先走 LaunchAgent）再打开。
set -uo pipefail

# 仓库内直接运行时按脚本位置推断；.app 里的副本由 make-app.sh 在前面注入 ASRWB_ROOT。
ROOT="${ASRWB_ROOT:-${0:A:h:h}}"
LABEL="com.kbrain.asrworkbench"
HOST="${ASRWB_HOST:-$("$ROOT/.venv/bin/python" "$ROOT/app/netcfg.py" host 2>/dev/null || echo 127.0.0.1)}"
PORT="${ASRWB_PORT:-8770}"
LOG="$ROOT/data/workbench.log"

lan_ip="$(/usr/sbin/ipconfig getifaddr en0 2>/dev/null || true)"
[[ -n "$lan_ip" ]] || lan_ip="$(/usr/sbin/ipconfig getifaddr en1 2>/dev/null || true)"
if [[ "$HOST" == "0.0.0.0" && -n "$lan_ip" ]]; then
  URL="http://$lan_ip:$PORT"
else
  URL="http://127.0.0.1:$PORT"
fi
CHECK="http://127.0.0.1:$PORT/api/state"

alive() { /usr/bin/curl -fsS -m 2 -o /dev/null "$CHECK" 2>/dev/null; }

open_page() { /usr/bin/open "$URL"; }

if alive; then
  open_page
  exit 0
fi

mkdir -p "$ROOT/data"
cd "$ROOT" || exit 1

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
else
  export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  export ASRWB_HOST="$HOST" ASRWB_PORT="$PORT"
  nohup "$ROOT/start.sh" >> "$LOG" 2>&1 &
  disown
fi

for i in {1..60}; do
  alive && { open_page; exit 0; }
  sleep 0.5
done

/usr/bin/osascript -e 'display alert "ASR 工作台启动失败" message "请查看 data/workbench.log"' >/dev/null 2>&1
exit 1
