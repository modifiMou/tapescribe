"""ffmpeg helpers: probing, merging and decoding to 16 kHz mono PCM."""

from __future__ import annotations

import asyncio
import io
import re
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .settings import FFMPEG

SAMPLE_RATE = 16000

_probe_cache: dict[tuple[str, float, int], dict] = {}
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)[^,]*,\s*(\d+) Hz,\s*([^,\n]+)")


async def _run(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    return proc.returncode or 0, err.decode("utf-8", "replace")


def _error_tail(stderr: str) -> str:
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    return "\n".join(lines[-3:]) or "ffmpeg 执行失败"


async def probe(path: Path) -> dict:
    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size)
    if key in _probe_cache:
        return _probe_cache[key]
    _, err = await _run(["-hide_banner", "-i", str(path)])
    info = {"duration": 0.0, "codec": "", "sample_rate": 0, "channels": ""}
    match = _DURATION_RE.search(err)
    if match:
        h, m, s = match.groups()
        info["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
    match = _AUDIO_RE.search(err)
    if match:
        info["codec"] = match.group(1)
        info["sample_rate"] = int(match.group(2))
        info["channels"] = match.group(3).strip()
    info["valid"] = bool(info["codec"])
    _probe_cache[key] = info
    return info


async def _run_with_progress(
    args: list[str], total_sec: float, on_progress: Optional[Callable[[float], None]]
) -> None:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_progress() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") and on_progress and total_sec > 0:
                value = line.split("=", 1)[1]
                if value.lstrip("-").isdigit():
                    on_progress(min(1.0, max(0.0, int(value) / 1e6 / total_sec)))

    try:
        assert proc.stderr is not None
        _, err = await asyncio.gather(read_progress(), proc.stderr.read())
        code = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if code != 0:
        raise RuntimeError(_error_tail(err.decode("utf-8", "replace")))


def merge_output_suffix(infos: list[dict], sources: list[Path]) -> tuple[str, bool]:
    """Return (suffix, can_stream_copy) for merging the given sources."""
    suffixes = {p.suffix.lower() for p in sources}
    formats = {(i["codec"], i["sample_rate"], i["channels"]) for i in infos}
    if len(suffixes) == 1 and len(formats) == 1:
        return suffixes.pop(), True
    return ".flac", False


async def merge(
    sources: list[Path],
    output: Path,
    copy: bool,
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    infos = [await probe(p) for p in sources]
    total = sum(i["duration"] for i in infos)
    if copy:
        list_file = output.with_name(output.name + ".concat.txt")
        lines = []
        for src in sources:
            escaped = str(src).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        args = ["-f", "concat", "-safe", "0", "-i", str(list_file), "-map", "0:a:0", "-c", "copy"]
        if output.suffix.lower() == ".wav":
            args += ["-rf64", "auto"]
        args.append(str(output))
        try:
            await _run_with_progress(args, total, on_progress)
        finally:
            list_file.unlink(missing_ok=True)
        return

    args: list[str] = []
    for src in sources:
        args += ["-i", str(src)]
    inputs = "".join(f"[{i}:a:0]" for i in range(len(sources)))
    args += [
        "-filter_complex", f"{inputs}concat=n={len(sources)}:v=0:a=1[out]",
        "-map", "[out]", "-c:a", "flac", str(output),
    ]
    await _run_with_progress(args, total, on_progress)


async def to_wav16k(
    source: Path, output: Path, on_progress: Optional[Callable[[float], None]] = None
) -> None:
    info = await probe(source)
    args = [
        "-i", str(source), "-map", "0:a:0", "-vn",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-rf64", "auto", str(output),
    ]
    await _run_with_progress(args, info["duration"], on_progress)


async def encode_aac(source_wav: Path, output: Path) -> None:
    """Small mono AAC copy used for in-browser playback and cloud upload."""
    code, err = await _run(
        ["-hide_banner", "-y", "-i", str(source_wav), "-c:a", "aac", "-b:a", "48k", str(output)]
    )
    if code != 0:
        raise RuntimeError(_error_tail(err))


def wav_duration(path: Path) -> float:
    """Duration of a wav file from its header, without loading the PCM body."""
    with path.open("rb") as handle:
        head = handle.read(256)
    marker = head.find(b"data", 12)
    if marker == -1 or marker + 8 > len(head):
        return 0.0
    size = int.from_bytes(head[marker + 4: marker + 8], "little")
    if size in (0, 0xFFFFFFFF):
        size = path.stat().st_size - (marker + 8)
    return size / 2 / SAMPLE_RATE


def open_wav16k(path: Path) -> np.ndarray:
    """Map the pipeline's own 16 kHz mono s16 wav as a read-only int16 array.

    Multi-hour recordings are several GB, so the PCM body stays on disk and is
    paged in by the caller instead of being copied into memory.
    """
    with path.open("rb") as handle:
        head = handle.read(512)
    marker = head.find(b"data", 12)
    if marker == -1:
        raise RuntimeError("音频解码结果异常：找不到 PCM 数据")
    offset = marker + 8
    size = int.from_bytes(head[marker + 4: marker + 8], "little")
    body = path.stat().st_size - offset
    if size in (0, 0xFFFFFFFF) or size > body:
        size = body
    return np.memmap(path, dtype="<i2", mode="r", offset=offset, shape=(size // 2,))


def wav_bytes(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(np.ascontiguousarray(pcm, dtype="<i2").tobytes())
    return buf.getvalue()
