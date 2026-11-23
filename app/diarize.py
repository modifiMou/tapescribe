"""Speaker diarization (who spoke when) and turning speaker turns into audio pieces."""

from __future__ import annotations

import os
import re
from typing import Callable, Optional

import numpy as np

from .media import SAMPLE_RATE
from .settings import EMBEDDING_MODEL, SEGMENTATION_MODEL

MAX_PIECE_SEC = 25.0
MERGE_GAP_SEC = 1.2
MIN_PIECE_SEC = 0.35
PAD_SEC = 0.12

# Clustering cost grows with the length of the audio passed to sherpa-onnx: one
# process() call over a 7-hour recording needs several GB and gets SIGKILLed by
# macOS under memory pressure. Long audio is diarized in chunks instead, each
# repeating the tail of the previous one so the speaker labels can be joined.
CHUNK_SEC = float(os.environ.get("DIARIZE_CHUNK_SEC") or 1800.0)
CHUNK_OVERLAP_SEC = float(os.environ.get("DIARIZE_CHUNK_OVERLAP_SEC") or 60.0)
# Without a target count the clustering over-segments long audio (100+ labels
# measured on one hour), so a chunk keeps at most this many of them.
AUTO_MAX_SPEAKERS = 8
MIN_ALIGN_SEC = 0.5


def models_ready() -> bool:
    return SEGMENTATION_MODEL.exists() and EMBEDDING_MODEL.exists()


def _engine(num_speakers: int):
    import sherpa_onnx

    if not models_ready():
        raise RuntimeError(
            f"说话人分离模型缺失，请确认存在:\n{SEGMENTATION_MODEL}\n{EMBEDDING_MODEL}"
        )
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(SEGMENTATION_MODEL)
            ),
            num_threads=4,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(EMBEDDING_MODEL), num_threads=4
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers > 0 else -1, threshold=0.5
        ),
        min_duration_on=0.25,
        min_duration_off=0.4,
    )
    if not config.validate():
        raise RuntimeError("说话人分离模型配置无效")
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def _run(engine, samples: np.ndarray, on_progress=None) -> list[dict]:
    def callback(done: int, total: int) -> int:
        if on_progress and total:
            on_progress(done / total)
        return 0

    result = engine.process(samples, callback=callback).sort_by_start_time()
    return [
        {"start": float(seg.start), "end": float(seg.end), "speaker": int(seg.speaker)}
        for seg in result
    ]


def chunks_for(total_sec: float, chunk_sec: Optional[float] = None) -> list[tuple[float, float]]:
    """Split [0, total_sec) into spans whose tails repeat the previous span's tail."""
    size = chunk_sec or CHUNK_SEC
    overlap = min(CHUNK_OVERLAP_SEC, size / 2)
    spans: list[tuple[float, float]] = []
    start = 0.0
    while start < total_sec:
        end = min(total_sec, start + size)
        spans.append((start, end))
        if end >= total_sec:
            break
        start = end - overlap
    return spans


def _cap(turns: list[dict], num_speakers: int) -> list[dict]:
    """Fold the smallest auto-detected clusters into the nearest remaining one.

    Only applies when the caller left the speaker count open; a fixed count is
    already bounded and folding it would undo what the caller asked for.
    """
    if num_speakers > 0 or not turns:
        return turns
    weight: dict[int, float] = {}
    for turn in turns:
        weight[turn["speaker"]] = weight.get(turn["speaker"], 0.0) + turn["end"] - turn["start"]
    if len(weight) <= AUTO_MAX_SPEAKERS:
        return turns
    keep = set(sorted(weight, key=lambda s: weight[s], reverse=True)[:AUTO_MAX_SPEAKERS])
    kept = [turn for turn in turns if turn["speaker"] in keep]
    for turn in turns:
        if turn["speaker"] in keep:
            continue
        mid = (turn["start"] + turn["end"]) / 2
        nearest = min(kept, key=lambda t: min(abs(t["start"] - mid), abs(t["end"] - mid)))
        turn["speaker"] = nearest["speaker"]
    return turns


class _Join:
    """Carry speaker labels from chunk to chunk.

    A chunk's clusters are numbered on their own, so each new chunk's labels have
    to be matched onto the labels used so far. The overlapping tail is diarized
    twice and matched by how much speech the two labellings share; clusters with
    no speech in that tail fall back to the labels heard most recently.
    """

    def __init__(self) -> None:
        self.turns: list[dict] = []

    def _shared(self, lo: float, hi: float) -> list[dict]:
        return [t for t in self.turns if t["end"] > lo and t["start"] < hi]

    def _recent(self, limit: int = 24) -> list[int]:
        """Labels of the last turns, most recent first."""
        return list(dict.fromkeys(t["speaker"] for t in reversed(self.turns[-limit:])))

    def mapping(self, local: list[dict], lo: float, own: float) -> dict[int, int]:
        shared = self._shared(lo, own)
        weights: dict[tuple[int, int], float] = {}
        for turn in local:
            a, b = max(turn["start"], lo), min(turn["end"], own)
            if b <= a:
                continue
            for other in shared:
                overlap = min(b, other["end"]) - max(a, other["start"])
                if overlap > 0:
                    key = (turn["speaker"], other["speaker"])
                    weights[key] = weights.get(key, 0.0) + overlap

        heard: dict[int, tuple[int, float]] = {}
        for (lid, gid), weight in weights.items():
            best = heard.get(lid)
            if best is None or weight > best[1]:
                heard[lid] = (gid, weight)

        labels = sorted({t["speaker"] for t in local})
        mapping: dict[int, int] = {}
        for lid in labels:
            gid, weight = heard.get(lid, (None, 0.0))
            if gid is not None and weight >= MIN_ALIGN_SEC:
                mapping[lid] = gid
        fresh = max((t["speaker"] for t in self.turns), default=-1) + 1
        recent = self._recent()
        for lid in labels:
            if lid in mapping:
                continue
            taken = set(mapping.values())
            # No shared speech to go by: continue with a label still in play.
            pick = next((gid for gid in recent if gid not in taken), None)
            mapping[lid] = fresh if pick is None else pick
            fresh += 1
        return mapping

    def absorb(self, local: list[dict], mapping: dict[int, int], own: float) -> None:
        for turn in local:
            turn["speaker"] = mapping[turn["speaker"]]
        for turn in local:
            if turn["end"] <= own:
                continue
            turn["start"] = max(turn["start"], own)
            self.turns.append(turn)


def diarize_file(
    pcm: np.ndarray,
    num_speakers: int,
    on_progress: Optional[Callable[[float], None]] = None,
    *,
    start: int = 0,
    carry: Optional[list[dict]] = None,
    on_chunk: Optional[Callable[[list[dict], int], None]] = None,
    chunk_sec: Optional[float] = None,
) -> list[dict]:
    """Diarize a whole recording chunk by chunk, keeping memory per chunk bounded.

    start/carry resume after chunk `start`, given previously finished turns;
    on_chunk(turns, chunks_done) is called after every chunk for checkpointing.
    """
    total = len(pcm) / SAMPLE_RATE
    spans = chunks_for(total, chunk_sec)
    engine = _engine(num_speakers)
    join = _Join()
    join.turns = list(carry or [])
    overlap = spans[0][1] - spans[1][0] if len(spans) > 1 else 0.0
    finished = sum(hi - (lo + overlap if i else lo) for i, (lo, hi) in enumerate(spans[:start]))

    for index in range(start, len(spans)):
        lo, hi = spans[index]
        own = lo if index == 0 else lo + overlap
        begin, end = int(lo * SAMPLE_RATE), int(hi * SAMPLE_RATE)
        samples = np.asarray(pcm[begin:end], dtype=np.float32) / 32768.0
        span = hi - own

        def tick(value: float, own=own, span=span) -> None:
            if on_progress:
                on_progress(min(1.0, (finished + span * value) / total))

        local = _cap(
            [{"start": t["start"] + lo, "end": t["end"] + lo, "speaker": t["speaker"]}
             for t in _run(engine, samples, tick)],
            num_speakers,
        )
        mapping = (
            join.mapping(local, lo, own) if join.turns
            else {t["speaker"]: t["speaker"] for t in local}
        )
        join.absorb(local, mapping, own)

        finished += span
        if on_progress:
            on_progress(min(1.0, finished / total))
        if on_chunk:
            on_chunk(join.turns, index + 1)

    # Each chunk may still have added labels the others never heard, so an open
    # speaker count is bounded once over the whole recording.
    return _cap(join.turns, num_speakers)


def _quiet_cut(pcm: np.ndarray, lo: float, hi: float) -> float:
    """Pick the quietest 50 ms frame between lo and hi seconds as a cut point."""
    frame = int(0.05 * SAMPLE_RATE)
    a, b = int(lo * SAMPLE_RATE), int(hi * SAMPLE_RATE)
    chunk = pcm[a:b].astype(np.float32)
    count = len(chunk) // frame
    if count < 2:
        return hi
    energy = (chunk[: count * frame].reshape(count, frame) ** 2).mean(axis=1)
    return lo + (int(energy.argmin()) + 0.5) * frame / SAMPLE_RATE


def build_pieces(turns: list[dict], pcm: np.ndarray) -> list[dict]:
    """Turn speaker turns into transcription pieces of at most MAX_PIECE_SEC."""
    total = len(pcm) / SAMPLE_RATE
    cleaned: list[dict] = []
    for turn in sorted(turns, key=lambda t: (t["start"], t["end"])):
        start, end = max(0.0, turn["start"]), min(total, turn["end"])
        if cleaned and start < cleaned[-1]["end"]:
            # Overlapping speech: keep the earlier turn, start this one after it.
            start = cleaned[-1]["end"]
        if end - start < 0.2:
            continue
        cleaned.append({"start": start, "end": end, "speaker": turn["speaker"]})

    merged: list[dict] = []
    for turn in cleaned:
        last = merged[-1] if merged else None
        if (
            last
            and last["speaker"] == turn["speaker"]
            and turn["start"] - last["end"] <= MERGE_GAP_SEC
            and turn["end"] - last["start"] <= MAX_PIECE_SEC
        ):
            last["end"] = turn["end"]
        else:
            merged.append(dict(turn))

    pieces: list[dict] = []
    for turn in merged:
        start, end = turn["start"], turn["end"]
        while end - start > MAX_PIECE_SEC:
            cut = _quiet_cut(pcm, start + MAX_PIECE_SEC - 8.0, start + MAX_PIECE_SEC)
            pieces.append({"start": start, "end": cut, "speaker": turn["speaker"]})
            start = cut
        if end - start >= MIN_PIECE_SEC:
            pieces.append({"start": start, "end": end, "speaker": turn["speaker"]})
    return pieces


def cut(pcm: np.ndarray, start: float, end: float) -> np.ndarray:
    a = max(0, int((start - PAD_SEC) * SAMPLE_RATE))
    b = min(len(pcm), int((end + PAD_SEC) * SAMPLE_RATE))
    return pcm[a:b]


_SENTENCE_END = "。！？!?…\n"
_CLAUSE_END = "，,；;、"
MAX_LINE_CHARS = 60


def split_text(text: str) -> list[str]:
    """Split a transcript into display lines at sentence ends, then long lines at commas."""
    text = re.sub(r"\s+", " ", text).strip()
    sentences: list[str] = []
    buf = ""
    for index, char in enumerate(text):
        buf += char
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if char in _SENTENCE_END and nxt not in _SENTENCE_END + "”’」）)":
            sentences.append(buf.strip())
            buf = ""
        elif char == "." and nxt == " " and len(buf) > 12:
            sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())

    lines: list[str] = []
    for sentence in sentences:
        while len(sentence) > MAX_LINE_CHARS:
            window = sentence[: MAX_LINE_CHARS]
            pos = max(window.rfind(c) for c in _CLAUSE_END)
            if pos < MAX_LINE_CHARS // 3:
                break
            lines.append(sentence[: pos + 1].strip())
            sentence = sentence[pos + 1:].strip()
        if sentence:
            lines.append(sentence)
    return lines


def spread_lines(text: str, start: float, end: float) -> list[dict]:
    """Split text into lines and share [start, end] between them by length."""
    parts = split_text(text)
    total_chars = sum(len(p) for p in parts)
    if not parts or total_chars == 0:
        return []
    out, cursor = [], start
    for part in parts:
        span = (end - start) * len(part) / total_chars
        out.append({"start": cursor, "end": cursor + span, "text": part})
        cursor += span
    return out


def speaker_for(start: float, end: float, turns: list[dict]) -> int:
    """Speaker with the largest overlap; falls back to the nearest turn."""
    best, best_overlap = None, 0.0
    for turn in turns:
        overlap = min(end, turn["end"]) - max(start, turn["start"])
        if overlap > best_overlap:
            best, best_overlap = turn["speaker"], overlap
    if best is not None:
        return best
    if not turns:
        return 0
    mid = (start + end) / 2
    nearest = min(turns, key=lambda t: min(abs(t["start"] - mid), abs(t["end"] - mid)))
    return nearest["speaker"]
