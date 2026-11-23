"""ASR workbench: audio library, merging, speaker-labelled transcription."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import diarize, jobs, media, netcfg, providers, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    settings.load_config()
    jobs.kill_stale_workers()
    jobs.resume(jobs.load_all(), find_provider)
    yield


app = FastAPI(title="ASR 工作台", lifespan=lifespan)

_PAGE_ASSETS = ("/", "/index.html", "/app.js", "/style.css")


@app.middleware("http")
async def revalidate_page_assets(request, call_next):
    """Let the browser revalidate the page shell so edits show up on a plain reload."""
    response = await call_next(request)
    if request.url.path in _PAGE_ASSETS:
        response.headers["Cache-Control"] = "no-cache"
    return response


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str) -> str:
    name = _UNSAFE.sub("_", Path(name).name).strip().lstrip(".")
    if not name:
        raise HTTPException(400, "文件名无效")
    return name


def discard(path: Path) -> Path:
    """Move a library file into data/trash so a mistaken delete stays recoverable."""
    settings.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = settings.TRASH_DIR / f"{stamp}-{path.name}"
    counter = 2
    while target.exists():
        target = settings.TRASH_DIR / f"{stamp}-{counter}-{path.name}"
        counter += 1
    shutil.move(str(path), target)
    return target


def audio_path(name: str) -> Path:
    path = settings.AUDIO_DIR / safe_name(name)
    if not path.is_file():
        raise HTTPException(404, f"音频不存在: {name}")
    return path


def free_name(name: str) -> Path:
    """Path in the library that does not overwrite an existing file."""
    path = settings.AUDIO_DIR / name
    stem, suffix, index = path.stem, path.suffix, 2
    while path.exists():
        path = settings.AUDIO_DIR / f"{stem}-{index}{suffix}"
        index += 1
    return path


def get_job(job_id: str) -> dict:
    job = jobs.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


def find_provider(provider_id: str) -> dict:
    for provider in settings.load_config()["providers"]:
        if provider["id"] == provider_id:
            return provider
    raise HTTPException(404, "识别模型不存在")


# ---- state ----


@app.get("/api/state")
async def state() -> dict:
    config = settings.load_config()
    return {
        "ffmpeg": settings.FFMPEG,
        "diarization_ready": diarize.models_ready(),
        "default_provider": config.get("default_provider"),
        "providers": [settings.public_provider(p) for p in config["providers"]],
        "audio_dir": str(settings.AUDIO_DIR),
        "network": netcfg.describe(settings.PORT),
    }


# ---- access scope ----


class NetworkUpdate(BaseModel):
    mode: str
    restart: bool = True


def _restart_self() -> None:
    """用新的监听地址重新执行本进程；排队中和进行中的任务会从断点重新排队。"""
    env = os.environ.copy()
    env["ASRWB_HOST"] = netcfg.MODES[netcfg.read_mode()]
    env["ASRWB_PORT"] = str(settings.PORT)
    os.execve(sys.executable, [sys.executable, "-m", "app.main"], env)


@app.get("/api/network")
async def get_network() -> dict:
    return netcfg.describe(settings.PORT)


@app.post("/api/network")
async def set_network(payload: NetworkUpdate) -> dict:
    try:
        mode = netcfg.save_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    restarting = payload.restart and netcfg.MODES[mode] != settings.HOST
    if restarting:
        asyncio.get_running_loop().call_later(0.5, _restart_self)
    info = netcfg.describe(settings.PORT, host=netcfg.MODES[mode])
    info.update({"saved_mode": mode, "restarting": restarting, "listening": settings.HOST})
    return info


# ---- audio library ----


@app.get("/api/files")
async def list_files() -> list[dict]:
    out = []
    for path in sorted(settings.AUDIO_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.suffix.lower() not in settings.AUDIO_EXTS:
            continue
        info = await media.probe(path)
        stat = path.stat()
        out.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime, **info})
    return out


@app.post("/api/files/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    saved = []
    for item in files:
        name = safe_name(item.filename or "audio")
        if Path(name).suffix.lower() not in settings.AUDIO_EXTS:
            raise HTTPException(400, f"不支持的文件类型: {name}")
        target = free_name(name)
        with target.open("wb") as handle:
            while chunk := await item.read(1 << 20):
                handle.write(chunk)
        saved.append(target.name)
    return {"saved": saved}


class ImportBody(BaseModel):
    path: str


@app.post("/api/files/import")
async def import_local(body: ImportBody) -> dict:
    """Copy audio from a folder or file on this Mac (e.g. the mounted recorder)."""
    source = Path(body.path.strip().strip("'\"")).expanduser()
    if not source.exists():
        raise HTTPException(400, f"路径不存在: {source}")
    candidates = sorted(source.iterdir()) if source.is_dir() else [source]
    picked = [
        p for p in candidates
        if p.is_file() and p.suffix.lower() in settings.AUDIO_EXTS and not p.name.startswith(".")
    ]
    if not picked:
        raise HTTPException(400, "该路径下没有找到音频文件")
    saved = []
    for item in picked:
        existing = settings.AUDIO_DIR / item.name
        if existing.exists() and existing.stat().st_size == item.stat().st_size:
            continue
        target = free_name(item.name)
        shutil.copy2(item, target)
        saved.append(target.name)
    return {"saved": saved, "skipped": len(picked) - len(saved)}


@app.get("/api/files/{name}/raw")
async def raw_file(name: str) -> FileResponse:
    return FileResponse(audio_path(name))


def names_in_active_jobs() -> set[str]:
    """Library file names that queued or running jobs still need on disk."""
    names: set[str] = set()
    for job in jobs.jobs.values():
        if job.get("status") not in ("queued", "running"):
            continue
        params = job.get("params") or {}
        for key in ("source", "output"):
            if params.get(key):
                names.add(Path(params[key]).name)
        for item in params.get("sources") or []:
            names.add(Path(item).name)
    return names


@app.delete("/api/files/{name}")
async def delete_file(name: str) -> dict:
    path = audio_path(name)
    if path.name in names_in_active_jobs():
        raise HTTPException(409, "该音频正在被进行中的任务使用")
    trash = discard(path)
    return {"ok": True, "trash": trash.name}


class DeleteFilesBody(BaseModel):
    names: list[str] = Field(min_length=1)


@app.post("/api/files/delete")
async def delete_files(body: DeleteFilesBody) -> dict:
    busy = names_in_active_jobs()
    deleted, missing, refused = [], [], []
    for name in body.names:
        try:
            safe = safe_name(name)
        except HTTPException:
            refused.append({"name": name, "reason": "文件名无效"})
            continue
        if safe in busy:
            refused.append({"name": safe, "reason": "正在被进行中的任务使用"})
            continue
        path = settings.AUDIO_DIR / safe
        if not path.is_file():
            missing.append(safe)
            continue
        discard(path)
        deleted.append(safe)
    return {"deleted": deleted, "missing": missing, "refused": refused}


# ---- merge ----


class MergeBody(BaseModel):
    files: list[str] = Field(min_length=2)
    name: str = ""


@app.post("/api/merge")
async def merge(body: MergeBody) -> dict:
    sources = [audio_path(name) for name in body.files]
    infos = [await media.probe(p) for p in sources]
    for path, info in zip(sources, infos):
        if not info["valid"]:
            raise HTTPException(400, f"无法读取音频: {path.name}")
    suffix, copy = media.merge_output_suffix(infos, sources)
    stem = Path(safe_name(body.name)).stem if body.name.strip() else f"{sources[0].stem}_合并{len(sources)}段"
    output = free_name(stem + suffix)
    job = jobs.start_merge(sources, output, copy, output.name)
    return jobs.summary(job)


# ---- transcribe ----


class TranscribeBody(BaseModel):
    files: list[str] = Field(min_length=1)
    provider_id: str
    language: str = "Chinese"
    speakers: int = Field(default=2, ge=0, le=8)
    context: str = ""


@app.post("/api/transcribe")
async def transcribe(body: TranscribeBody) -> list[dict]:
    provider = find_provider(body.provider_id)
    if body.speakers != 1 and not diarize.models_ready():
        raise HTTPException(400, "说话人分离模型缺失，无法区分人物")
    sources = [audio_path(name) for name in body.files]
    options = {
        "language": body.language.strip(),
        "speakers": body.speakers,
        "context": body.context.strip(),
    }
    return [jobs.summary(jobs.start_transcribe(p, provider, options)) for p in sources]


# ---- jobs ----


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    ordered = sorted(jobs.jobs.values(), key=lambda j: j["created"], reverse=True)
    return [jobs.summary(j) for j in ordered]


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str) -> dict:
    job = get_job(job_id)
    return {k: v for k, v in job.items() if not k.startswith("_")}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    get_job(job_id)
    return {"ok": jobs.cancel(job_id)}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    job = get_job(job_id)
    provider = find_provider(job["params"].get("provider_id", ""))
    try:
        jobs.retry(job, provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return jobs.summary(job)


@app.post("/api/jobs/retry-failed")
async def retry_failed() -> dict:
    skipped = 0
    retried = []
    for job in sorted(jobs.jobs.values(), key=lambda j: j["created"]):
        if job["type"] != "transcribe" or job["status"] != "failed":
            continue
        try:
            jobs.retry(job, find_provider(job["params"].get("provider_id", "")))
        except (ValueError, HTTPException):
            skipped += 1
            continue
        retried.append(job["id"])
    return {"retried": len(retried), "skipped": skipped, "jobs": retried}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    get_job(job_id)
    jobs.delete(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/audio")
async def job_audio(job_id: str) -> FileResponse:
    get_job(job_id)
    path = jobs.job_dir(job_id) / "audio.m4a"
    if not path.exists():
        raise HTTPException(404, "音频尚未准备好")
    return FileResponse(path, media_type="audio/mp4")


class Line(BaseModel):
    start: float
    end: float
    speaker: int
    text: str


class TranscriptBody(BaseModel):
    speakers: dict[str, str]
    lines: list[Line]


@app.put("/api/jobs/{job_id}/transcript")
async def update_transcript(job_id: str, body: TranscriptBody) -> dict:
    job = get_job(job_id)
    if job["status"] != "done" or "transcript" not in job:
        raise HTTPException(400, "任务尚未完成，不能编辑")
    job["transcript"]["speakers"] = {
        key: (value.strip() or "?")[:20] for key, value in body.speakers.items()
    }
    job["transcript"]["lines"] = [line.model_dump() for line in body.lines]
    jobs.save(job)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/export")
async def export(job_id: str, fmt: str = "txt") -> Response:
    job = get_job(job_id)
    if fmt not in ("txt", "srt", "md", "json"):
        raise HTTPException(400, "不支持的导出格式")
    content, mime = jobs.export(job, fmt)
    filename = quote(f"{Path(job['title']).stem}.{fmt}")
    return Response(
        content,
        media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


# ---- providers ----


@app.post("/api/providers")
async def upsert_provider(raw: dict) -> dict:
    config = settings.load_config()
    previous = next((p for p in config["providers"] if p["id"] == raw.get("id")), None)
    try:
        provider = settings.normalize_provider(raw, previous)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if previous:
        config["providers"][config["providers"].index(previous)] = provider
    else:
        config["providers"].append(provider)
    if raw.get("make_default") or not config.get("default_provider"):
        config["default_provider"] = provider["id"]
    settings.save_config(config)
    return settings.public_provider(provider)


@app.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict:
    config = settings.load_config()
    remaining = [p for p in config["providers"] if p["id"] != provider_id]
    if len(remaining) == len(config["providers"]):
        raise HTTPException(404, "识别模型不存在")
    if not remaining:
        raise HTTPException(400, "至少保留一个识别模型")
    config["providers"] = remaining
    if config.get("default_provider") == provider_id:
        config["default_provider"] = remaining[0]["id"]
    settings.save_config(config)
    return {"ok": True}


@app.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> dict:
    config = find_provider(provider_id)
    try:
        provider = providers.create(config)
        async with httpx.AsyncClient(timeout=15) as client:
            return {"ok": True, "message": await provider.test(client)}
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"连接失败: {str(exc) or exc.__class__.__name__}"}
    except (RuntimeError, KeyError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}


app.mount("/", StaticFiles(directory=settings.STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")
