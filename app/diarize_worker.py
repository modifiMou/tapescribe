"""Standalone diarization worker process.

sherpa-onnx keeps the GIL for the whole engine.process() call, so running it in a
thread freezes the whole HTTP server for minutes on multi-hour recordings. It runs
in a child process instead: progress goes to stdout, the result to a JSON file.

Finished chunks are checkpointed next to the result, so a run that is killed
halfway resumes instead of starting over.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from . import diarize, media


def write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint(
    path: Path, samples: int, speakers: int, chunk_sec: float
) -> tuple[int, Optional[list[dict]]]:
    """Chunks already finished for this exact audio and speaker setting, if any."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        turns = payload["turns"]
        chunks = int(payload["chunks"])
        same = (
            int(payload.get("samples", -1)) == samples
            and int(payload.get("speakers", -1)) == speakers
            and float(payload.get("chunk_sec", -1)) == chunk_sec
            and float(payload.get("overlap_sec", -1)) == diarize.CHUNK_OVERLAP_SEC
        )
    except (OSError, ValueError, KeyError, TypeError):
        return 0, None
    if not same or not isinstance(turns, list) or chunks <= 0:
        return 0, None
    return chunks, turns


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: app.diarize_worker <wav> <num_speakers> <out.json>", file=sys.stderr)
        return 2
    wav = Path(argv[1])
    num_speakers = int(argv[2])
    out = Path(argv[3])
    chunk_sec = diarize.CHUNK_SEC
    checkpoint = out.with_suffix(".chunks.json")

    pcm = media.open_wav16k(wav)
    samples = int(len(pcm))
    start, carry = load_checkpoint(checkpoint, samples, num_speakers, chunk_sec)
    if start:
        print(f"resume after {start} chunks", file=sys.stderr, flush=True)

    def progress(value: float) -> None:
        print(f"progress {value:.4f}", flush=True)

    def save_chunk(turns: list[dict], chunks: int) -> None:
        write_atomic(checkpoint, {
            "samples": samples,
            "speakers": num_speakers,
            "chunk_sec": chunk_sec,
            "overlap_sec": diarize.CHUNK_OVERLAP_SEC,
            "chunks": chunks,
            "turns": turns,
        })

    turns = diarize.diarize_file(
        pcm, num_speakers, progress, start=start, carry=carry, on_chunk=save_chunk,
        chunk_sec=chunk_sec,
    )
    write_atomic(out, {"samples": samples, "turns": turns})
    checkpoint.unlink(missing_ok=True)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
