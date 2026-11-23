#!/bin/zsh
# 安装两个登录级 LaunchAgent：主服务（登录自启 + 退出自动拉起）和心跳看门狗（卡死自动重启）。
set -uo pipefail

ROOT="${0:A:h:h}"
LABEL="com.kbrain.asrworkbench"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/asrWorkbench.log"
PORT="${ASRWB_PORT:-8770}"
WD="$HOME/Library/Application Support/asrWorkbench"

# 解释器和依赖目录从当前 .venv 解析，模板里不写死任何本机路径。
PY="$("$ROOT/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')" || exit 1
SITE="$("$ROOT/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')" || exit 1

render() {  # $1 模板，$2 输出
  "$ROOT/.venv/bin/python" - "$1" "$2" "$ROOT" "$HOME" "$PY" "$SITE" "$WD" "$PORT" <<'PY' || exit 1
import os
import sys
from xml.sax.saxutils import escape

tpl, out, root, home, python, site, wd, port = sys.argv[1:9]
start = os.environ.get("ASR_START_SCRIPT", "").strip()
extra = ""
if start:
    extra = f'    <key>ASR_START_SCRIPT</key>\n    <string>{escape(start)}</string>'

values = {
    "__ROOT__": root,
    "__HOME__": home,
    "__PYTHON__": python,
    "__SITE__": site,
    "__WATCHDOG__": os.path.join(wd, "watchdog.sh"),
    "__PORT__": port,
}
text = open(tpl, encoding="utf-8").read()
for key, value in values.items():
    text = text.replace(key, escape(value))
text = text.replace("__ASR_START_ENV__", extra)
open(out, "w", encoding="utf-8").write(text)
PY
}

# 看门狗脚本放在 ~/Library 下：launchd 里的 zsh 读不到 ~/Documents 里的脚本
install_watchdog() {
  local wd_label="$LABEL.watchdog"
  mkdir -p "$WD"
  cp "$ROOT/desktop/watchdog.sh" "$WD/watchdog.sh"
  chmod +x "$WD/watchdog.sh"
  render "$ROOT/desktop/$wd_label.plist.in" "$HOME/Library/LaunchAgents/$wd_label.plist"
  launchctl bootout "gui/$(id -u)/$wd_label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$wd_label.plist" \
    && echo "已安装心跳看门狗（每 30 秒检查一次）"
}

mkdir -p "$(dirname "$PLIST")" "$HOME/Library/Logs" "$ROOT/data"
render "$ROOT/desktop/$LABEL.plist.in" "$PLIST"

# data/workbench.log 指向 LaunchAgent 的日志，保留原有 tail 路径
if [[ ! -L "$ROOT/data/workbench.log" ]]; then
  touch "$LOG"
  [[ -f "$ROOT/data/workbench.log" ]] && cat "$ROOT/data/workbench.log" >> "$LOG"
  rm -f "$ROOT/data/workbench.log"
  ln -s "$LOG" "$ROOT/data/workbench.log"
fi

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

# 旧实例可能卡死并忽略 TERM，10 秒内不退就强杀，否则占着 8770 让新实例起不来
pkill -f "$ROOT/.venv/bin/python -m app.main" >/dev/null 2>&1 || true
pkill -f "python3.12 -m app.main" >/dev/null 2>&1 || true
for i in {1..20}; do
  pgrep -f "python3.12 -m app.main" >/dev/null 2>&1 || break
  sleep 0.5
done
pkill -9 -f "python3.12 -m app.main" >/dev/null 2>&1 || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST" || exit 1

for i in {1..40}; do
  if /usr/bin/curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/state"; then
    echo "已安装并启动 $LABEL，日志 $LOG"
    install_watchdog
    exit 0
  fi
  sleep 0.5
done

echo "启动失败，检查 $PORT 是否被占用、以及是否有卡死的旧进程：" >&2
echo "  lsof -nP -iTCP:$PORT -sTCP:LISTEN   # 有输出就 pkill -9 掉再重试" >&2
echo "最近日志：" >&2
tail -5 "$LOG" >&2
exit 1
