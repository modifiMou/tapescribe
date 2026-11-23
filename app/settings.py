"""Paths, runtime settings and the persisted provider configuration."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from . import netcfg

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ASRWB_DATA", ROOT / "data"))
AUDIO_DIR = DATA_DIR / "audio"
JOBS_DIR = DATA_DIR / "jobs"
TRASH_DIR = DATA_DIR / "trash"
CONFIG_FILE = DATA_DIR / "config.json"
MODELS_DIR = Path(os.environ.get("ASRWB_MODELS", ROOT / "models"))
STATIC_DIR = ROOT / "static"

HOST = netcfg.resolve_host()
PORT = int(os.environ.get("ASRWB_PORT", "8770"))

SEGMENTATION_MODEL = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMBEDDING_MODEL = MODELS_DIR / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"

# Optional launcher for the recognition service, run by the queue when /health stops
# answering. Unset by default; point ASR_START_SCRIPT at your own script to enable it.
_start_script = os.environ.get("ASR_START_SCRIPT", "").strip()
ASR_START_SCRIPT = Path(_start_script).expanduser() if _start_script else None

AUDIO_EXTS = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".mp4", ".mov", ".mkv", ".webm", ".amr", ".aiff", ".aif",
}

PROVIDER_TYPES = ("mlx", "openai", "dashscope_filetrans")


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/ffmpeg",
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("找不到 ffmpeg，请安装或通过 FFMPEG 环境变量指定路径")


FFMPEG = find_ffmpeg()


def ensure_dirs() -> None:
    for directory in (AUDIO_DIR, JOBS_DIR, TRASH_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def default_config() -> dict:
    return {
        "default_provider": "local-qwen3-asr",
        "providers": [
            {
                "id": "local-qwen3-asr",
                "name": "本地 Qwen3-ASR-1.7B",
                "type": "mlx",
                "base_url": "http://127.0.0.1:8765",
                "model": "Qwen3-ASR-1.7B",
                "api_key": "",
                "api_key_file": "",
                "rate_per_min": 50,
                "extra": {},
            },
            {
                "id": "dashscope-filetrans",
                "name": "阿里云百炼 录音文件识别",
                "type": "dashscope_filetrans",
                "base_url": "https://dashscope.aliyuncs.com/api/v1",
                "model": "qwen-audio-3.1-asr-flash-filetrans",
                "api_key": "",
                "api_key_file": "",
                "rate_per_min": 0,
                "extra": {},
            },
        ],
    }


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(config.get("providers"), list):
                return config
        except ValueError:
            pass
    config = default_config()
    save_config(config)
    return config


def save_config(config: dict) -> None:
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def normalize_provider(raw: dict, previous: dict | None = None) -> dict:
    """Validate a provider coming from the UI; a blank key keeps the stored one."""
    kind = str(raw.get("type", "")).strip()
    if kind not in PROVIDER_TYPES:
        raise ValueError(f"不支持的接入类型: {kind}")
    name = str(raw.get("name", "")).strip()
    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    if not name or not base_url:
        raise ValueError("名称和接口地址不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("接口地址需要以 http:// 或 https:// 开头")
    extra = raw.get("extra") or {}
    if not isinstance(extra, dict):
        raise ValueError("附加参数必须是 JSON 对象")
    api_key = str(raw.get("api_key", "")).strip()
    if not api_key and previous and not raw.get("clear_api_key"):
        api_key = previous.get("api_key", "")
    try:
        rate = max(0, int(raw.get("rate_per_min") or 0))
    except (TypeError, ValueError):
        raise ValueError("每分钟请求上限必须是数字") from None
    return {
        "id": str(raw.get("id") or "").strip() or f"p-{uuid.uuid4().hex[:8]}",
        "name": name,
        "type": kind,
        "base_url": base_url,
        "model": str(raw.get("model", "")).strip(),
        "api_key": api_key,
        "api_key_file": str(raw.get("api_key_file", "")).strip(),
        "rate_per_min": rate,
        "extra": extra,
    }


def public_provider(provider: dict) -> dict:
    """Provider as shown to the browser: the key itself never leaves the server."""
    shown = {k: v for k, v in provider.items() if k != "api_key"}
    shown["has_api_key"] = bool(provider.get("api_key"))
    return shown


def resolve_api_key(provider: dict) -> str:
    key = provider.get("api_key", "")
    if key:
        return key
    key_file = provider.get("api_key_file", "")
    if key_file:
        path = Path(key_file).expanduser()
        if not path.exists():
            raise RuntimeError(f"密钥文件不存在: {path}")
        content = path.read_text(encoding="utf-8").strip()
        for line in content.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
        return content
    return ""
