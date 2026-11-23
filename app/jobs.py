"""Background jobs: merging recordings and transcribing them with speaker labels."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import diarize, media, providers
from .settings import ASR_START_SCRIPT, JOBS_DIR, ROOT

SPEAKER_NAMES = "ABCDEFGH"
ACTIVE = ("queued", "running")

jobs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}
_queue: Optional[asyncio.Queue] = None
_worker: Optional[asyncio.Task] = None


class Canceled(Exception):
    pass


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def save(job: dict) -> None:
    directory = job_dir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "job.json.tmp"
    # Keys starting with "_" hold runtime-only data such as provider credentials.
    stored = {k: v for k, v in job.items() if not k.startswith("_")}
    tmp.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    tmp.replace(directory / "job.json")


def kill_stale_workers() -> None:
    """Diarization children outlive a restart; kill the orphaned ones so they cannot fight a resumed job."""
    listing = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=False
    ).stdout
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[1] != "1":
            continue
        if re.search(r"-m app\.diarize_worker .*/data/jobs/", parts[2]):
            try:
                os.kill(int(parts[0]), signal.SIGTERM)
            except (OSError, ValueError):
                pass


def load_all() -> list[str]:
    """Load stored jobs; return ids that were mid-flight so startup can requeue them."""
    interrupted = []
    for path in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if job.get("status") in ACTIVE:
            done = bool((job.get("transcript") or {}).get("lines"))
            job.update(
                status="queued",
                stage="继续排队中" if done else "排队中",
                error="",
                finished=None,
            )
            if not done:
                job["progress"] = 0.0
            save(job)
            interrupted.append(job["id"])
        jobs[job["id"]] = job
    return interrupted


def resume(job_ids: list[str], resolve_provider) -> None:
    """Requeue jobs interrupted by a restart; their provider config is not stored in job.json."""
    for job_id in sorted(job_ids, key=lambda i: jobs[i]["created"]):
        job = jobs[job_id]
        source = Path(job["params"].get("source", ""))
        if not source.is_file() and not (job_dir(job_id) / "audio.m4a").is_file():
            _finish(job, "failed", f"源音频已删除：{source.name}")
            continue
        try:
            job["_provider"] = resolve_provider(job["params"].get("provider_id", ""))
        except Exception as exc:  # noqa: BLE001 - a missing provider fails only this job
            _finish(job, "failed", f"识别模型不可用：{exc}")
            continue
        _schedule(job)


def summary(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k != "transcript" and not k.startswith("_")}
    transcript = job.get("transcript")
    if transcript:
        out["line_count"] = len(transcript.get("lines", []))
        out["duration"] = transcript.get("duration", 0)
    return out


def new_job(kind: str, title: str, params: dict) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "type": kind,
        "title": title,
        "status": "queued",
        "stage": "排队中",
        "progress": 0.0,
        "error": "",
        "created": time.time(),
        "finished": None,
        "params": params,
    }
    jobs[job["id"]] = job
    save(job)
    return job


def _finish(job: dict, status: str, error: str = "") -> None:
    job["status"] = status
    job["error"] = error
    job["finished"] = time.time()
    if status == "done":
        job["progress"] = 1.0
        job["stage"] = "完成"
    save(job)


async def _guard(job: dict, coro) -> None:
    """Run a job body and record how it ended."""
    try:
        job["status"] = "running"
        save(job)
        await coro
        _finish(job, "done")
    except asyncio.CancelledError:
        _finish(job, "canceled", "已取消")
    except Exception as exc:  # noqa: BLE001 - every failure is reported on the job
        _finish(job, "failed", str(exc) or exc.__class__.__name__)
    finally:
        _tasks.pop(job["id"], None)


# ---- merge ----


def start_merge(sources: list[Path], output: Path, copy: bool, title: str) -> dict:
    job = new_job(
        "merge",
        title,
        {"sources": [p.name for p in sources], "output": output.name, "copy": copy},
    )

    async def body() -> None:
        job["stage"] = "直接拼接（不重新编码）" if copy else "重新编码为 FLAC 后拼接"

        def progress(value: float) -> None:
            job["progress"] = value

        try:
            await media.merge(sources, output, copy, progress)
        except BaseException:
            output.unlink(missing_ok=True)
            raise

    _tasks[job["id"]] = asyncio.create_task(_guard(job, body()))
    return job


# ---- transcribe ----


def start_transcribe(source: Path, provider_config: dict, options: dict) -> dict:
    global _queue, _worker
    job = new_job(
        "transcribe",
        source.name,
        {
            "source": str(source),
            "provider_id": provider_config["id"],
            "provider_name": provider_config["name"],
            "model": provider_config.get("model", ""),
            **options,
        },
    )
    job["_provider"] = provider_config
    _schedule(job)
    return job


def _schedule(job: dict) -> None:
    global _queue, _worker
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_run_queue())
    _queue.put_nowait(job["id"])


def retry(job: dict, provider_config: dict) -> dict:
    """Requeue a failed transcription, keeping the pieces it already finished."""
    if job["type"] != "transcribe" or job["status"] not in ("failed", "canceled"):
        raise ValueError("只有失败的转写任务可以重试")
    source = Path(job["params"].get("source", ""))
    if not source.is_file() and not (job_dir(job["id"]) / "audio.m4a").is_file():
        raise ValueError(f"源音频已不在 data/audio/，任务目录也没有缓存音频：{source.name}")
    done = len((job.get("transcript") or {}).get("lines") or [])
    job.update(
        status="queued",
        stage=f"排队中（已有 {done} 行，继续识别）" if done else "排队中",
        error="",
        finished=None,
    )
    if not done:
        job.pop("transcript", None)
        job["progress"] = 0.0
    job["_provider"] = provider_config
    save(job)
    _schedule(job)
    return job


async def _start_service() -> bool:
    """Run the launcher of the recognition service; it is a no-op while the port is already taken."""
    if ASR_START_SCRIPT is None or not ASR_START_SCRIPT.is_file():
        return False
    try:
        # Own session: the service must not die with the workbench when launchd
        # restarts this job (it kills the job's process group).
        proc = await asyncio.create_subprocess_exec(
            str(ASR_START_SCRIPT),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.wait_for(proc.wait(), timeout=120)
    except (OSError, asyncio.TimeoutError):
        return False
    return True


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


async def _restart_hung_service(provider, label: str, stage: Callable[[str], None]) -> bool:
    """Kill a service that keeps its port open but stops answering, then relaunch it."""
    pids = await asyncio.to_thread(local_listener_pids, provider.base)
    if not pids:
        return False
    stage(f"{label} · 识别服务在跑但不响应，重启它")
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    await asyncio.sleep(5)
    for pid in await asyncio.to_thread(local_listener_pids, provider.base):
        _signal(pid, signal.SIGKILL)
    await asyncio.sleep(2)
    return await _start_service()


async def _await_online(provider, label: str, stage: Callable[[str], None]) -> None:
    """Block until the recognition back end answers again; an outage must not kill a job."""
    deadline = time.monotonic() + SERVICE_WAIT_MIN * 60
    waiting_since = time.monotonic()
    launches = 0
    restarted = False
    while True:
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as probe:
                await provider.test(probe)
            return
        except providers.ProviderError as exc:
            if re.search(r"HTTP 4\d\d", str(exc)):
                raise RuntimeError(f"识别服务拒绝请求：{exc}") from exc
            if time.monotonic() > deadline:
                raise RuntimeError(f"识别服务 {SERVICE_WAIT_MIN} 分钟内没有恢复") from None
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"识别服务 {SERVICE_WAIT_MIN} 分钟内没有恢复"
                ) from None
        except Exception as exc:  # noqa: BLE001 - anything else is a config problem, waiting will not help
            raise RuntimeError(f"识别服务不可用：{_transport_cause(exc)}") from exc
        hung = time.monotonic() - waiting_since > HUNG_RESTART_SEC
        if not restarted and hung and isinstance(provider, providers.MlxProvider):
            restarted = True
            if await _restart_hung_service(provider, label, stage):
                await asyncio.sleep(20)
                continue
        if launches < 2 and isinstance(provider, providers.MlxProvider):
            launches += 1
            stage(f"{label} · 识别服务离线，正在启动")
            if await _start_service():
                await asyncio.sleep(20)
                continue
        stage(f"{label} · 识别服务离线，等待恢复")
        await asyncio.sleep(15)


async def _run_job(job: dict) -> None:
    if job["type"] == "transcribe" and job.get("_provider"):
        await _await_online(
            providers.create(dict(job["_provider"])),
            "等待识别服务",
            lambda text: job.__setitem__("stage", text),
        )
    await _transcribe(job)


async def _run_queue() -> None:
    assert _queue is not None
    while True:
        job_id = await _queue.get()
        job = jobs.get(job_id)
        if not job or job["status"] != "queued":
            continue
        task = asyncio.create_task(_guard(job, _run_job(job)))
        _tasks[job_id] = task
        await asyncio.wait([task])


def cancel(job_id: str) -> bool:
    job = jobs.get(job_id)
    if not job or job["status"] not in ACTIVE:
        return False
    task = _tasks.get(job_id)
    if task:
        task.cancel()
    else:
        _finish(job, "canceled", "已取消")
    return True


def delete(job_id: str) -> None:
    cancel(job_id)
    jobs.pop(job_id, None)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def _relabel(turns: list[dict]) -> None:
    """Number speakers by order of first appearance so the first voice is A."""
    order: dict[int, int] = {}
    for turn in turns:
        turn["speaker"] = order.setdefault(turn["speaker"], len(order))


async def _diarize_once(
    wav: Path,
    num_speakers: int,
    out: Path,
    on_progress: Callable[[float], None],
) -> tuple[int, str]:
    """Run the worker once; returns its exit code and stderr."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.diarize_worker", str(wav), str(num_speakers), str(out),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT),
    )

    async def read_progress() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            field, _, value = raw.decode("utf-8", "replace").strip().partition(" ")
            if field == "progress":
                try:
                    on_progress(float(value))
                except ValueError:
                    pass

    assert proc.stderr is not None
    progress_task = asyncio.create_task(read_progress())
    err_task = asyncio.create_task(proc.stderr.read())
    try:
        err = await err_task
        code = await proc.wait()
        await progress_task
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    finally:
        for pending in (progress_task, err_task):
            pending.cancel()
    return code, err.decode("utf-8", "replace")


def _worker_error(code: int, err: str) -> str:
    if code == 0:
        return ""
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"信号 {-code}"
        hint = "，内存或系统压力下被强制终止" if -code == signal.SIGKILL else ""
        return f"说话人分离进程被系统终止：{name}{hint}"
    tail = [line for line in err.strip().splitlines() if line.strip()]
    return "\n".join(tail[-3:]) or f"说话人分离进程退出码 {code}，无错误输出"


async def _diarize_in_child(
    wav: Path,
    num_speakers: int,
    out: Path,
    on_progress: Callable[[float], None],
) -> list[dict]:
    """Diarize in a child process: sherpa-onnx holds the GIL while it works.

    The worker checkpoints finished chunks, so a run that dies (the OS killing it
    under memory pressure, for one) is retried and continues where it stopped.
    """
    for attempt in range(DIARIZE_ATTEMPTS):
        code, err = await _diarize_once(wav, num_speakers, out, on_progress)
        if code == 0:
            return json.loads(out.read_text(encoding="utf-8"))["turns"]
        out.unlink(missing_ok=True)
        message = _worker_error(code, err)
        # Killed by a signal or silent death: retrying picks up the checkpoint.
        if (code < 0 or not err.strip()) and attempt + 1 < DIARIZE_ATTEMPTS:
            await asyncio.sleep(2)
            continue
        raise RuntimeError(message)
    raise RuntimeError("说话人分离失败")


def _cached_turns(path: Path, duration: float) -> Optional[list[dict]]:
    """Reuse the diarization of the same audio so a resumed job skips it."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        turns = payload["turns"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(turns, list) or not turns:
        return None
    if abs(float(payload.get("samples", 0)) / media.SAMPLE_RATE - duration) > 1.0:
        return None
    return turns


DIARIZE_ATTEMPTS = 3
PIECE_RETRY_DELAYS = (5, 10, 20, 40, 60)
SERVICE_WAIT_MIN = 30
HUNG_RESTART_SEC = 300
PROBE_TIMEOUT = httpx.Timeout(connect=5, read=15, write=15, pool=5)

TRANSPORT_CAUSES = {
    "ConnectError": "连接失败",
    "ConnectTimeout": "连接超时",
    "ReadTimeout": "读取超时",
    "WriteTimeout": "发送超时",
    "PoolTimeout": "连接池等待超时",
    "RemoteProtocolError": "服务端断开连接",
}


def _transport_cause(exc: Exception) -> str:
    """Readable cause for httpx transport errors, whose timeout messages are empty."""
    detail = str(exc).strip()
    text = TRANSPORT_CAUSES.get(type(exc).__name__, type(exc).__name__)
    return f"{text}（{detail}）" if detail else text


def local_listener_pids(base_url: str) -> list[int]:
    """PIDs listening on this machine for base_url; empty when it points elsewhere."""
    parts = urllib.parse.urlsplit(base_url)
    host, port = parts.hostname or "", parts.port
    if not port:
        return []
    try:
        addresses = {"127.0.0.1", "::1"} | {
            info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)
        }
    except OSError:
        addresses = {"127.0.0.1", "::1"}
    if host not in addresses and host != socket.gethostname():
        return []
    found = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True, check=False,
    )
    return [int(pid) for pid in found.stdout.split() if pid.isdigit()]


async def _piece_text(
    provider, client, clip: bytes, language: str, context: str, label: str, stage
) -> str:
    """Survive a recognition service restart: quick retries, then wait for it to come back."""
    for wait in PIECE_RETRY_DELAYS:
        try:
            return await provider.transcribe_piece(client, clip, language, context)
        except httpx.TransportError:
            stage(f"{label} · 识别服务连不上，{wait} 秒后重试")
            await asyncio.sleep(wait)
    stage(f"{label} · 识别服务离线，等待恢复")
    await _await_online(provider, label, stage)
    try:
        return await provider.transcribe_piece(client, clip, language, context)
    except httpx.TransportError as exc:
        raise RuntimeError(f"识别服务连不上：{_transport_cause(exc)}") from exc


async def _transcribe(job: dict) -> None:
    params = job["params"]
    provider = providers.create(job.pop("_provider"))
    directory = job_dir(job["id"])
    source = Path(params["source"])
    if not source.is_file():
        cached = directory / "audio.m4a"
        if not cached.is_file():
            raise RuntimeError(f"源音频已删除：{source.name}")
        source = cached
    wav_path = directory / "audio16k.wav"
    turns_path = directory / "turns.json"
    num_speakers = int(params.get("speakers", 2))
    language = params.get("language", "")
    context = params.get("context", "")

    def stage(text: str, progress: Optional[float] = None) -> None:
        job["stage"] = text
        if progress is not None:
            job["progress"] = progress

    try:
        stage("解码音频", 0.0)
        await media.to_wav16k(source, wav_path, lambda v: job.__setitem__("progress", v * 0.04))
        await media.encode_aac(wav_path, directory / "audio.m4a")
        duration = await asyncio.to_thread(media.wav_duration, wav_path)
        if duration < 0.3:
            raise RuntimeError("音频太短或没有音轨")

        async def find_turns() -> list[dict]:
            if num_speakers == 1:
                return [{"start": 0.0, "end": duration, "speaker": 0}]
            cached = _cached_turns(turns_path, duration)
            if cached is not None:
                _relabel(cached)
                return cached
            turns = await _diarize_in_child(
                wav_path, num_speakers, turns_path,
                lambda v: job.__setitem__("progress", 0.05 + v * 0.2),
            )
            _relabel(turns)
            return turns

        previous = job.get("transcript") or {}
        kept = [line for line in previous.get("lines") or [] if isinstance(line, dict)]
        if kept and abs(float(previous.get("duration") or 0) - duration) <= 1.0:
            transcript = {
                **previous,
                "source": source.name,
                "duration": duration,
                "language": language,
                "lines": kept,
            }
            resume_from = float(previous.get("done_until") or 0.0)
            if not resume_from:
                # Older transcripts carry no cursor; the last line ends where the last piece did.
                resume_from = max(float(line.get("end") or 0.0) for line in kept)
            stage(f"接着上次继续：已有 {len(kept)} 行", max(0.25, job.get("progress", 0.0)))
        else:
            transcript = {
                "source": source.name,
                "duration": duration,
                "language": language,
                "speakers": {},
                "lines": [],
                "done_until": 0.0,
            }
            resume_from = 0.0
        job["transcript"] = transcript

        def name_speakers(turns: list[dict]) -> None:
            ids = sorted({t["speaker"] for t in turns}) or [0]
            transcript["speakers"] = {
                str(i): SPEAKER_NAMES[i] if i < len(SPEAKER_NAMES) else f"说话人{i + 1}"
                for i in ids
            }

        async with httpx.AsyncClient(timeout=providers.TIMEOUT) as client:
            if provider.mode == "file":
                stage("区分说话人 + 云端识别", 0.05)
                turns_task = asyncio.create_task(find_turns())
                try:
                    sentences = await provider.transcribe_file(
                        client, directory / "audio.m4a", language, context,
                        lambda text: stage(text),
                    )
                    stage("区分说话人")
                    turns = await turns_task
                finally:
                    turns_task.cancel()
                name_speakers(turns)
                for sentence in sentences:
                    for line in diarize.spread_lines(
                        sentence["text"], sentence["start"], sentence["end"]
                    ):
                        line["speaker"] = diarize.speaker_for(line["start"], line["end"], turns)
                        transcript["lines"].append(line)
            else:
                stage("区分说话人" if num_speakers != 1 else "切分音频", 0.05)
                turns = await find_turns()
                name_speakers(turns)
                pcm = await asyncio.to_thread(media.open_wav16k, wav_path)
                pieces = diarize.build_pieces(turns, pcm)
                if not pieces:
                    raise RuntimeError("没有检测到人声")
                voiced = sum(p["end"] - p["start"] for p in pieces)
                done = 0.0
                last_save = time.monotonic()
                for index, piece in enumerate(pieces):
                    span = piece["end"] - piece["start"]
                    if piece["end"] <= resume_from + 1e-6:
                        done += span
                        continue
                    label = f"识别中 {index + 1}/{len(pieces)}"
                    stage(label)
                    clip = media.wav_bytes(diarize.cut(pcm, piece["start"], piece["end"]))
                    text = await _piece_text(provider, client, clip, language, context, label, stage)
                    for line in diarize.spread_lines(text, piece["start"], piece["end"]):
                        line["speaker"] = piece["speaker"]
                        transcript["lines"].append(line)
                    transcript["done_until"] = round(piece["end"], 3)
                    done += span
                    job["progress"] = 0.25 + 0.75 * done / voiced
                    if time.monotonic() - last_save > 10:
                        save(job)
                        last_save = time.monotonic()

        for line in transcript["lines"]:
            line["start"] = round(line["start"], 2)
            line["end"] = round(line["end"], 2)
    finally:
        wav_path.unlink(missing_ok=True)


# ---- export ----


def clock(seconds: float, millis: bool = False) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{base},{ms:03d}" if millis else base


def export(job: dict, fmt: str) -> tuple[str, str]:
    transcript = job.get("transcript") or {"lines": [], "speakers": {}}
    names = transcript.get("speakers", {})
    lines = transcript.get("lines", [])

    def who(line: dict) -> str:
        return names.get(str(line.get("speaker", 0)), "?")

    if fmt == "json":
        return json.dumps(transcript, ensure_ascii=False, indent=2), "application/json"
    if fmt == "srt":
        blocks = [
            f"{i}\n{clock(l['start'], True)} --> {clock(l['end'], True)}\n{who(l)}：{l['text']}\n"
            for i, l in enumerate(lines, 1)
        ]
        return "\n".join(blocks), "text/plain"
    if fmt == "md":
        out = [f"# {transcript.get('source', job['title'])}", ""]
        previous = None
        for line in lines:
            if line.get("speaker") != previous:
                out += ["", f"**{who(line)}**  `{clock(line['start'])}`", ""]
                previous = line.get("speaker")
            out.append(f"{line['text']}  ")
        return "\n".join(out).strip() + "\n", "text/markdown"
    text = "\n".join(
        f"[{clock(l['start'])} - {clock(l['end'])}] {who(l)}：{l['text']}" for l in lines
    )
    return text + "\n", "text/plain"
