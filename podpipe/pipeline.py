"""Orchestration: feed sync and per-episode processing.

Each stage caches its output to disk, so re-running after a crash or a merge
tweak skips the expensive model passes.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from pathlib import Path
from typing import Any, Callable

from . import asr, audio, diarize, feeds, merge, players, youtube
from .config import Config
from .store import STATUS_DONE, STATUS_DOWNLOADED, STATUS_ERROR, Store

Log = Callable[[str], None]


def fix_names(words: list[dict[str, Any]], cfg: Config, log: Log) -> list[dict[str, Any]]:
    """Respell misheard player names, if a roster has been built."""
    roster = players.load(cfg.players_path)
    if not roster:
        return words
    words, fixes = players.correct(words, roster, cfg.name_match)
    for (heard, fixed), n in fixes.most_common():
        log(f"  name: {heard} -> {fixed} (x{n})")
    return words


def sync_feeds(store: Store, log: Log = print) -> int:
    """Poll every feed, record episodes we haven't seen. Returns new count."""
    total = 0
    for feed in store.feeds():
        try:
            title, episodes = feeds.parse_feed(feed["url"])
        except Exception as exc:  # noqa: BLE001 - one bad feed shouldn't stop the rest
            log(f"  ! {feed['url']}: {exc}")
            continue
        if title and title != feed["title"]:
            store.add_feed(feed["url"], title)
        new = sum(
            store.add_episode(
                feed["id"], ep["guid"], ep["title"], ep["published"], ep["audio_url"]
            )
            # oldest first, so ids rise with recency even for feeds without dates
            for ep in reversed(episodes)
        )
        total += new
        log(f"  {title or feed['url']}: {new} new / {len(episodes)} in feed")
    return total


class Processor:
    """Holds the loaded models across episodes.

    Model load and CoreML compilation cost several seconds each, so constructing
    this once per batch rather than once per episode is most of the difference
    on a long backfill.
    """

    def __init__(self, cfg: Config, log: Log = print):
        self.cfg = cfg
        self.log = log
        self._diarizer: diarize.Diarizer | None = None

    @property
    def diarizer(self) -> diarize.Diarizer:
        if self._diarizer is None:
            self.log("  loading diarizer…")
            self._diarizer = diarize.Diarizer(device="auto", warmup=True, quiet=True)
        return self._diarizer

    def process(self, store: Store, episode: sqlite3.Row, force: bool = False) -> None:
        eid = episode["id"]
        label = episode["title"] or episode["guid"]
        self.log(f"[{eid}] {label}")

        try:
            wav = self._ensure_audio(store, episode)
            asr_result = self._ensure_asr(episode, wav, force)
            segments = self._ensure_diarization(eid, wav, force)

            words = fix_names(asr.flatten_words(asr_result), self.cfg, self.log)
            turns = merge.merge(
                words,
                segments,
                max_gap=self.cfg.max_turn_gap,
                orphan_gap=self.cfg.orphan_word_gap,
            )
            store.replace_turns(eid, turns)
            store.set_status(eid, STATUS_DONE)

            speakers = diarize.speaker_count(segments)
            self.log(f"  done: {len(turns)} turns, {speakers} speakers, {len(words)} words")

            if not self.cfg.keep_audio:
                wav.unlink(missing_ok=True)
                if episode["audio_path"]:
                    Path(episode["audio_path"]).unlink(missing_ok=True)

        except Exception as exc:  # noqa: BLE001
            self.log(f"  ! failed: {exc}")
            store.set_status(eid, STATUS_ERROR, error=traceback.format_exc(limit=3))

    # ---- stages ----------------------------------------------------------

    def _ensure_audio(self, store: Store, episode: sqlite3.Row) -> Path:
        src = Path(episode["audio_path"]) if episode["audio_path"] else None
        if src is None or not src.exists():
            self.log("  downloading…")
            src = feeds.download(episode["audio_url"], self.cfg.audio_dir, episode["title"])
            store.set_status(
                episode["id"],
                STATUS_DOWNLOADED,
                audio_path=str(src),
                duration=audio.duration_seconds(src),
            )
        return audio.to_wav16k(src, self.cfg.audio_dir)

    def _ensure_asr(self, episode: sqlite3.Row, wav: Path, force: bool) -> dict[str, Any]:
        cached = self.cfg.raw_dir / f"{episode['id']}.asr.json"
        if cached.exists() and not force:
            return json.loads(cached.read_text())
        result = self._youtube_captions(episode, wav)
        if result is None:
            self.log("  transcribing…")
            result = asr.transcribe(wav, self.cfg.asr_model, self.cfg.language)
        cached.write_text(json.dumps(result))
        return result

    def _youtube_captions(self, episode: sqlite3.Row, wav: Path) -> dict[str, Any] | None:
        """YouTube auto-captions in place of Whisper, when present and dense enough."""
        if not (self.cfg.use_captions and youtube.is_youtube(episode["audio_url"])):
            return None
        result = youtube.fetch_captions(episode["audio_url"])
        if not youtube.captions_reliable(result, audio.duration_seconds(wav)):
            self.log("  no reliable youtube captions, falling back to whisper")
            return None
        self.log("  using youtube captions")
        return result

    def _ensure_diarization(self, eid: int, wav: Path, force: bool) -> list[dict[str, Any]]:
        cached = self.cfg.raw_dir / f"{eid}.diar.json"
        if cached.exists() and not force:
            return json.loads(cached.read_text())
        self.log("  diarizing…")
        segments = self.diarizer.run(wav)
        cached.write_text(json.dumps(segments))
        return segments


def remerge(store: Store, cfg: Config, eid: int, log: Log = print) -> int:
    """Re-run only the merge stage from cached model output. Cheap — use it to
    tune max_turn_gap without paying for ASR again."""
    asr_path = cfg.raw_dir / f"{eid}.asr.json"
    diar_path = cfg.raw_dir / f"{eid}.diar.json"
    if not (asr_path.exists() and diar_path.exists()):
        raise FileNotFoundError(f"no cached model output for episode {eid}")

    words = fix_names(asr.flatten_words(json.loads(asr_path.read_text())), cfg, log)
    segments = json.loads(diar_path.read_text())
    turns = merge.merge(words, segments, cfg.max_turn_gap, cfg.orphan_word_gap)
    store.replace_turns(eid, turns)
    store.set_status(eid, STATUS_DONE)
    return len(turns)
