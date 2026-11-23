"""访问范围设置：仅本机（localhost）或局域网（lan），保存在 data/network.json。

启动脚本用 `.venv/bin/python app/netcfg.py host` 读取同一个值，避免两处各写一份默认。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ASRWB_DATA", ROOT / "data"))
CONFIG_FILE = DATA_DIR / "network.json"

MODES = {"localhost": "127.0.0.1", "lan": "0.0.0.0"}
MODE_LABELS = {"localhost": "仅本机", "lan": "局域网"}
DEFAULT_MODE = "localhost"


def read_mode() -> str:
    try:
        mode = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("mode")
    except (OSError, ValueError):
        mode = None
    return mode if mode in MODES else DEFAULT_MODE


def save_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"未知的访问范围: {mode}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": mode}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)
    return mode


def resolve_host() -> str:
    """ASRWB_HOST 优先（命令行或 launchd 临时覆盖），否则按保存的模式决定监听地址。"""
    return os.environ.get("ASRWB_HOST", "").strip() or MODES[read_mode()]


def lan_ip() -> str | None:
    for iface in ("en0", "en1"):
        try:
            done = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        ip = done.stdout.strip()
        if done.returncode == 0 and ip:
            return ip
    return None


def describe(port: int, host: str | None = None) -> dict:
    """给前端和日志用的监听状态；host 可传期望值，用于保存后尚未重启时展示。"""
    mode = read_mode()
    host = host or resolve_host()
    ip = lan_ip() if host == "0.0.0.0" else None
    return {
        "mode": mode,
        "label": MODE_LABELS[mode],
        "host": host,
        "port": port,
        "lan_ip": ip,
        "lan_url": f"http://{ip}:{port}" if ip else None,
    }


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "mode"
    if command == "host":
        print(resolve_host())
    elif command == "ip":
        print(lan_ip() or "")
    else:
        print(read_mode())
