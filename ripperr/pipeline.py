"""Orchestration: feed sync and per-episode processing.

Each stage caches its output to disk, so re-running after a crash or a merge
tweak skips the expensive model passes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import asr, audio, diarize, feeds, glossary, merge
from .config import Config, stage_key
from .models import Correction, Episode, Turn
from .store import STATUS_DOWNLOADED, STATUS_ERROR, STATUS_NEW, Store

Log = Callable[[str], None]


def _diarization_key(segments: list[dict[str, Any]]) -> str:
    payload = json.dumps(segments, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def sync_feeds(store: Store, log: Log = print) -> int:
    """Poll every feed and record episodes we haven't seen.

    Feeds are expected to present newest entries first. For an existing feed,
    only entries before the first known guid are imported; this avoids silently
    backfilling an entire historical feed when a sync cursor is first created.
    """
    total = 0
    for feed in store.feeds():
        known = store.source_guids(feed.id)
        refresh = store.source_guids_without_published(feed.id)
        try:
            title, episodes = feeds.parse_feed(feed.url, known, refresh)
        except Exception as exc:  # noqa: BLE001 - one bad feed shouldn't stop the rest
            log(f"  ! {feed.url}: {exc}")
            continue
        if title and title != feed.title:
            store.add_feed(feed.url, title)
        if known:
            fresh = []
            for episode in episodes:
                if episode["source_guid"] in known:
                    break
                fresh.append(episode)
            # Refresh metadata for all stored episodes too, so a newly available
            # source summary reaches existing rows without importing the whole
            # historical feed.
            episodes = fresh + [episode for episode in episodes if episode["source_guid"] in known]
        else:
            episodes = episodes[:1]
        new = sum(
            store.add_episode(
                feed.id, ep["source_guid"], ep["title"], ep["published"], ep["audio_url"],
                ep.get("source_url"), ep.get("summary"),
            )
            # oldest first, so ids rise with recency even for feeds without dates
            for ep in reversed(episodes)
        )
        total += new
        log(f"  {title or feed.url}: {new} new / {len(episodes)} in feed")
    return total


def backfill_feed(store: Store, feed_id: int, count: int, log: Log = print) -> int:
    """Record the feed's `count` newest entries, whatever sync has seen so far.

    Already known entries are refreshed, not duplicated, so repeating a backfill
    is harmless. Raises if the feed cannot be loaded.
    """
    if count < 1:
        raise ValueError("backfill count must be at least 1")
    feed = store.feed(feed_id)
    _, episodes = feeds.parse_feed(
        feed.url, store.source_guids(feed_id), store.source_guids_without_published(feed_id),
    )
    new = sum(
        store.add_episode(
            feed_id, ep["source_guid"], ep["title"], ep["published"], ep["audio_url"],
            ep.get("source_url"), ep.get("summary"),
        )
        for ep in reversed(episodes[:count])
    )
    log(f"  {feed.title or feed.url}: backfilled {new} new / {min(count, len(episodes))} requested")
    return new


def _read_cache(path: Path) -> Any | None:
    """Cached JSON, or None if it is missing or unreadable (say, cut short by a
    crash). An unreadable cache is a miss, never an error."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, data: Any) -> None:
    """Write through a temp file and rename, so an interruption never leaves a
    partial file at the real path."""
    payload = json.dumps(data)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def merge_and_save(
    store: Store,
    cfg: Config,
    episode: Episode,
    asr_result: dict[str, Any],
    segments: list[dict[str, Any]],
    terms: list[str],
    log: Log,
    *,
    speaker_embeddings: Mapping[str, Sequence[float]] | None = None,
) -> tuple[int, int]:
    """Apply the glossary, merge words with speakers, store the transcript.
    Returns (turns, words)."""
    words = asr.flatten_words(asr_result)
    corrections: list[Correction] = []
    if terms:
        words, fixes = glossary.correct(words, terms, cfg.glossary_match)
        corrections = [Correction(h, f, n) for (h, f), n in fixes.most_common()]
        for c in corrections:
            log(f"  name: {c.heard} -> {c.fixed} (x{c.count})")

    merged = merge.merge(words, segments, cfg.max_turn_gap, cfg.orphan_word_gap)
    turns = [Turn(i, t["speaker"], t["start"], t["end"], t["text"]) for i, t in enumerate(merged)]
    store.replace_turns(
        episode.id,
        turns,
        corrections,
        diarization_key=_diarization_key(segments),
        speaker_embeddings=speaker_embeddings,
    )  # also marks the episode done
    return len(turns), len(words)


class Processor:
    """Holds the loaded models across episodes.

    Model load and CoreML compilation cost several seconds each, so constructing
    this once per batch rather than once per episode is most of the difference
    on a long backfill.
    """

    def __init__(self, cfg: Config, log: Log = print, terms: list[str] | None = None):
        self.cfg = cfg
        self.log = log
        self.terms = terms or []
        self._diarizer: diarize.Diarizer | None = None

    @property
    def diarizer(self) -> diarize.Diarizer:
        if self._diarizer is None:
            self.log("  loading diarizer…")
            self._diarizer = diarize.Diarizer(
                device=self.cfg.device,
                backend=self.cfg.diarization_backend,
                model=self.cfg.diarization_model,
                token=self.cfg.diarization_token,
                warmup=True,
                quiet=True,
            )
        return self._diarizer

    def process(self, store: Store, episode: Episode, force: bool = False) -> None:
        label = episode.title or episode.guid
        self.log(f"[{episode.id}] {label}")

        try:
            wav = self._ensure_audio(store, episode)
            asr_result = self._ensure_asr(episode, wav, force)
            segments = self._ensure_diarization(episode, wav, force)
            embeddings = _read_embedding_cache(self.cfg.raw_path(episode.guid, "embed"))
            store.set_raw_keys(episode.id, None, None)  # this machine's own models

            n_turns, n_words = merge_and_save(
                store,
                self.cfg,
                episode,
                asr_result,
                segments,
                self.terms,
                self.log,
                speaker_embeddings=embeddings,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ! failed: {exc}")
            store.set_status(episode.id, STATUS_ERROR, error=traceback.format_exc(limit=3))
            return

        # The transcript is committed and the episode is done. Nothing from here on
        # may change that, so a cleanup problem is logged, not recorded as a failure.
        self.log(f"  done: {n_turns} turns, {diarize.speaker_count(segments)} speakers, {n_words} words")
        if not self.cfg.keep_audio:
            try:
                self._delete_audio(store, episode, wav)
            except Exception as exc:  # noqa: BLE001
                self.log(f"  ! could not delete audio: {exc}")

    # ---- stages ----------------------------------------------------------

    def _ensure_audio(self, store: Store, episode: Episode) -> Path:
        return prepare_audio(store, self.cfg, episode, self.log)

    def _delete_audio(self, store: Store, episode: Episode, wav: Path) -> None:
        delete_audio(store, episode, wav)

    def _ensure_asr(self, episode: Episode, wav: Path, force: bool) -> dict[str, Any]:
        cached = self.cfg.raw_path(episode.guid, "asr")
        if not force:
            hit = _read_cache(cached)
            if hit is not None:
                return hit
            if cached.exists():
                self.log("  asr cache unreadable, redoing")
        self.log("  transcribing…")
        result = asr.transcribe(
            wav,
            self.cfg.asr_model,
            self.cfg.language,
            self.cfg.asr_backend,
            self.cfg.device,
        )
        _write_cache(cached, result)
        return result

    def _ensure_diarization(self, episode: Episode, wav: Path, force: bool) -> list[dict[str, Any]]:
        cached = self.cfg.raw_path(episode.guid, "diar")
        if not force:
            hit = _read_cache(cached)
            if hit is not None:
                return hit
            if cached.exists():
                self.log("  diarization cache unreadable, redoing")
        self.log("  diarizing…")
        if getattr(self.diarizer, "backend", None) == "senko":
            segments, embeddings = self.diarizer.run_with_embeddings(wav)
        else:
            segments, embeddings = self.diarizer.run(wav), {}
        _write_cache(cached, segments)
        _write_cache(self.cfg.raw_path(episode.guid, "embed"), embeddings)
        return segments


def prepare_audio(store: Store, cfg: Config, episode: Episode, log: Log = print) -> Path:
    """Download the episode if needed and return its 16 kHz mono WAV.

    The episode becomes `downloaded`, and so claimable by a worker, only once
    the WAV exists: a worker must never fetch audio still being converted."""
    src = Path(episode.audio_path) if episode.audio_path else None
    if src is None or not src.exists():
        log("  downloading…")
        src = feeds.download(episode.audio_url, cfg.audio_dir, episode.title)
        store.set_status(
            episode.id,
            episode.status,
            audio_path=str(src),
            duration=audio.duration_seconds(src),
        )
    wav = audio.to_wav16k(src, cfg.audio_dir)
    if episode.status in (STATUS_NEW, STATUS_ERROR):  # a retried failure goes back in the queue too
        store.set_status(episode.id, STATUS_DOWNLOADED)
    return wav


def delete_audio(store: Store, episode: Episode, wav: Path) -> None:
    wav.unlink(missing_ok=True)
    current = store.episode(episode.guid)  # `episode` may predate a download
    if current and current.audio_path:
        Path(current.audio_path).unlink(missing_ok=True)
        store.clear_audio(episode.id)  # only once the file is really gone


def raw_output_path(store: Store, cfg: Config, episode: Episode, kind: str) -> Path:
    """Where the model output the episode's transcript came from is cached.
    Embeddings belong to the diarization run."""
    asr_key, diar_key = store.raw_keys(episode.guid)
    key = asr_key if kind == "asr" else diar_key
    if key:
        return cfg.raw_path(episode.guid, kind, key)
    own = cfg.raw_path(episode.guid, kind)
    if own.exists():
        return own
    # ponytail: transcripts merged before schema 9 have no recorded key and may
    # come from models this machine no longer has; take the newest cache file.
    found = sorted(
        cfg.raw_dir.glob(f"{cfg.episode_key(episode.guid)}.{kind}-*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    return found[-1] if found else own


def remerge(
    store: Store, cfg: Config, episode: Episode, terms: list[str], log: Log = print
) -> None:
    """Re-run only the glossary and merge stages from cached model output. Cheap:
    use it to tune max_turn_gap or refresh the glossary without paying for ASR."""
    asr_result = _read_cache(raw_output_path(store, cfg, episode, "asr"))
    segments = _read_cache(raw_output_path(store, cfg, episode, "diar"))
    if asr_result is None or segments is None:
        raise FileNotFoundError(f"no usable cached model output for episode {episode.guid}")

    embeddings = _read_embedding_cache(raw_output_path(store, cfg, episode, "embed"))
    merge_and_save(
        store,
        cfg,
        episode,
        asr_result,
        segments,
        terms,
        log,
        speaker_embeddings=embeddings,
    )


WORKER_SCHEMAS = (1,)


@dataclass(frozen=True)
class WorkResult:
    """A worker's model output, checked against docs/worker-contract.md."""
    asr_key: str
    asr: dict[str, Any]  # {"segments": [...]}, the shape the ASR cache stores
    diar_key: str
    segments: list[dict[str, Any]]
    embedding_space: str | None
    embeddings: dict[str, list[float]] | None


def parse_work_result(body: Mapping[str, Any]) -> WorkResult:
    """Validate a result body. Raises ValueError naming the first problem."""
    if body.get("schema") not in WORKER_SCHEMAS:
        raise ValueError(f"schema must be one of {list(WORKER_SCHEMAS)}")
    asr_body, diar_body = body.get("asr"), body.get("diarization")
    if not isinstance(asr_body, dict) or not isinstance(diar_body, dict):
        raise ValueError("asr and diarization objects are required")

    asr_segments = _list_of_dicts(asr_body.get("segments"), "asr.segments")
    for seg in asr_segments:
        _times(seg, "asr segment", allow_none=True)
        if not isinstance(seg.get("text", ""), str):
            raise ValueError("asr segment text must be a string")
        for word in _list_of_dicts(seg.get("words") or [], "asr words"):
            _times(word, "asr word", allow_none=True)
            if not isinstance(word.get("word", ""), str):
                raise ValueError("asr word must be a string")

    segments = []
    for seg in _list_of_dicts(diar_body.get("segments"), "diarization.segments"):
        start, end = _times(seg, "diarization segment")
        if not isinstance(seg.get("speaker"), str) or not seg["speaker"]:
            raise ValueError("diarization segment speaker must be a non-empty string")
        segments.append({"start": start, "end": end, "speaker": seg["speaker"]})
    segments.sort(key=lambda seg: (seg["start"], seg["end"]))

    space, embeddings = None, None
    embed_body = diar_body.get("embeddings")
    if embed_body is not None:
        speakers = embed_body.get("speakers") if isinstance(embed_body, dict) else None
        space = embed_body.get("space") if isinstance(embed_body, dict) else None
        if not isinstance(space, str) or not isinstance(speakers, dict):
            raise ValueError("diarization.embeddings needs a space string and a speakers object")
        embeddings = {}
        for speaker, vector in speakers.items():
            if not isinstance(vector, list) or not vector or not all(_is_number(v) for v in vector):
                raise ValueError("each speaker embedding must be a non-empty list of numbers")
            embeddings[speaker] = [float(v) for v in vector]

    return WorkResult(
        asr_key=stage_key(_model_config(asr_body.get("config"), "asr")),
        asr={"segments": asr_segments},
        diar_key=stage_key(_model_config(diar_body.get("config"), "diarization")),
        segments=segments,
        embedding_space=space,
        embeddings=embeddings,
    )


def apply_work_result(
    store: Store, cfg: Config, episode: Episode, result: WorkResult, terms: list[str], log: Log = print
) -> tuple[int, int]:
    """Cache a worker's output under its own model keys and merge it, exactly as
    if this machine had produced it. Returns (turns, words)."""
    embeddings = result.embeddings
    if embeddings is not None and result.embedding_space != cfg.embedding_space:
        log(f"  ! dropping speaker embeddings from space {result.embedding_space!r}; "
            f"this database uses {cfg.embedding_space!r}")
        embeddings = None
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    _write_cache(cfg.raw_path(episode.guid, "asr", result.asr_key), result.asr)
    _write_cache(cfg.raw_path(episode.guid, "diar", result.diar_key), result.segments)
    _write_cache(cfg.raw_path(episode.guid, "embed", result.diar_key), embeddings or {})
    store.set_raw_keys(episode.id, result.asr_key, result.diar_key)
    return merge_and_save(
        store, cfg, episode, result.asr, result.segments, terms, log, speaker_embeddings=embeddings,
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _list_of_dicts(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must be a list of objects")
    return value


def _times(item: Mapping[str, Any], name: str, *, allow_none: bool = False) -> tuple[float, float]:
    start, end = item.get("start"), item.get("end")
    for value in (start, end):
        if not (_is_number(value) or (allow_none and value is None)):
            raise ValueError(f"{name} start and end must be numbers")
    return start, end


def _model_config(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("backend"), str):
        raise ValueError(f"{name}.config must be an object with a backend")
    if not all(v is None or isinstance(v, (str, int, float)) for v in value.values()):
        raise ValueError(f"{name}.config values must be strings, numbers or null")
    return value


def _read_embedding_cache(path: Path) -> dict[str, list[float]] | None:
    value = _read_cache(path)
    if not isinstance(value, dict):
        return None
    return value
