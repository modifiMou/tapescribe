#!/bin/zsh
# Start the ASR workbench. The access scope (localhost / lan) is stored in data/network.json;
# ASRWB_HOST / ASRWB_PORT override it for one-off runs.
set -euo pipefail

ROOT="${0:A:h}"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"
export ASRWB_PORT="${ASRWB_PORT:-8770}"
if [[ -z "${ASRWB_HOST:-}" ]]; then
  ASRWB_HOST="$("$ROOT/.venv/bin/python" "$ROOT/app/netcfg.py" host 2>/dev/null || echo 127.0.0.1)"
fi
export ASRWB_HOST

lan_ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
[[ -n "$lan_ip" ]] || lan_ip="$(ipconfig getifaddr en1 2>/dev/null || true)"

echo "ASR 工作台"
echo "  本机: http://127.0.0.1:$ASRWB_PORT"
if [[ "$ASRWB_HOST" == "0.0.0.0" ]]; then
  [[ -n "$lan_ip" ]] && echo "  内网: http://$lan_ip:$ASRWB_PORT"
else
  echo "  范围: 仅本机（切换局域网在页面「识别模型设置」里改，或改 data/network.json）"
fi
echo "  监听: $ASRWB_HOST:$ASRWB_PORT"

exec "$ROOT/.venv/bin/python" -m app.main
