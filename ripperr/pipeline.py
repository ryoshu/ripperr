"""Orchestration: feed sync and per-episode processing.

Each stage caches its output to disk, so re-running after a crash or a merge
tweak skips the expensive model passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import asr, audio, diarize, feeds, glossary, merge
from .config import Config
from .models import Correction, Episode, Turn
from .store import STATUS_DOWNLOADED, STATUS_ERROR, Store

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
        src = Path(episode.audio_path) if episode.audio_path else None
        if src is None or not src.exists():
            self.log("  downloading…")
            src = feeds.download(episode.audio_url, self.cfg.audio_dir, episode.title)
            store.set_status(
                episode.id,
                STATUS_DOWNLOADED,
                audio_path=str(src),
                duration=audio.duration_seconds(src),
            )
        return audio.to_wav16k(src, self.cfg.audio_dir)

    def _delete_audio(self, store: Store, episode: Episode, wav: Path) -> None:
        wav.unlink(missing_ok=True)
        current = store.episode(episode.guid)  # `episode` predates a download in this run
        if current and current.audio_path:
            Path(current.audio_path).unlink(missing_ok=True)
            store.clear_audio(episode.id)  # only once the file is really gone

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


def remerge(
    store: Store, cfg: Config, episode: Episode, terms: list[str], log: Log = print
) -> None:
    """Re-run only the glossary and merge stages from cached model output. Cheap:
    use it to tune max_turn_gap or refresh the glossary without paying for ASR."""
    asr_result = _read_cache(cfg.raw_path(episode.guid, "asr"))
    segments = _read_cache(cfg.raw_path(episode.guid, "diar"))
    if asr_result is None or segments is None:
        raise FileNotFoundError(f"no usable cached model output for episode {episode.guid}")

    embeddings = _read_embedding_cache(cfg.raw_path(episode.guid, "embed"))
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


def _read_embedding_cache(path: Path) -> dict[str, list[float]] | None:
    value = _read_cache(path)
    if not isinstance(value, dict):
        return None
    return value
