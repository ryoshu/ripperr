"""Orchestration: feed sync and per-episode processing.

Each stage caches its output to disk, so re-running after a crash or a merge
tweak skips the expensive model passes.
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

from . import asr, audio, diarize, feeds, glossary, merge
from .config import Config
from .models import Correction, Episode, Turn
from .store import STATUS_DOWNLOADED, STATUS_ERROR, Store

Log = Callable[[str], None]


def sync_feeds(store: Store, log: Log = print) -> int:
    """Poll every feed, record episodes we haven't seen. Returns new count."""
    total = 0
    for feed in store.feeds():
        try:
            title, episodes = feeds.parse_feed(feed.url)
        except Exception as exc:  # noqa: BLE001 - one bad feed shouldn't stop the rest
            log(f"  ! {feed.url}: {exc}")
            continue
        if title and title != feed.title:
            store.add_feed(feed.url, title)
        new = sum(
            store.add_episode(
                feed.id, ep["source_guid"], ep["title"], ep["published"], ep["audio_url"]
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
    store.replace_turns(episode.id, turns, corrections)  # also marks the episode done
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
            self._diarizer = diarize.Diarizer(device="auto", warmup=True, quiet=True)
        return self._diarizer

    def process(self, store: Store, episode: Episode, force: bool = False) -> None:
        label = episode.title or episode.guid
        self.log(f"[{episode.id}] {label}")

        try:
            wav = self._ensure_audio(store, episode)
            asr_result = self._ensure_asr(episode, wav, force)
            segments = self._ensure_diarization(episode, wav, force)

            n_turns, n_words = merge_and_save(
                store, self.cfg, episode, asr_result, segments, self.terms, self.log
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
        result = asr.transcribe(wav, self.cfg.asr_model, self.cfg.language)
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
        segments = self.diarizer.run(wav)
        _write_cache(cached, segments)
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

    merge_and_save(store, cfg, episode, asr_result, segments, terms, log)
