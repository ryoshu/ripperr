"""The public interface. The CLI and any other caller (e.g. another service) go
through this class and never touch the storage layer.

    with Ripperr() as rip:
        rip.add_feed("https://example.com/feed.xml")
        rip.sync()
        rip.process(limit=3, glossary=["Bhayshul Tuten", "Drake Maye"])
        for ep in rip.episodes(status="done", updated_since=last_seen):
            transcript = rip.transcript(ep.guid)

Episodes are identified by `guid`, which is stable across storage backends. An
int refers to the local `Episode.id` instead, which the CLI uses for convenience.
`Episode.revision` increases whenever a transcript is rewritten, so a consumer can
tell when it needs to re-read one.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config, default_config
from .glossary import load as load_glossary
from .models import (
    Change,
    Episode,
    Feed,
    Hit,
    SpeakerEmbedding,
    SpeakerMatch,
    SpeakerName,
    Transcript,
)
from .pipeline import Log, Processor, remerge, sync_feeds
from .store import Store


_CACHE_FILE = re.compile(r"^(?P<episode_key>[0-9a-f]{12})\.(?:asr|diar|embed)-[0-9a-f]{12}\.json$")


class ProcessingBusyError(RuntimeError):
    """A destructive operation was attempted during model processing."""


class ChangeLogPrunedError(RuntimeError):
    """A consumer cursor is older than the retained change log."""

    def __init__(self, pruned_through: int):
        self.pruned_through = pruned_through
        super().__init__(
            f"change log was pruned through sequence {pruned_through}; re-bootstrap required"
        )


class Ripperr:
    def __init__(self, cfg: Config | None = None, *, store: Store | None = None, log: Log = print):
        self.cfg = cfg or default_config()
        if store is None:
            self.cfg.ensure_dirs()
            store = Store(self.cfg.db_path)
        self.store = store
        self.log = log

    def __enter__(self) -> Ripperr:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.store.close()

    # ---- ingest ----------------------------------------------------------

    def add_feed(self, url: str, title: str | None = None) -> Feed:
        return self.store.add_feed(url, title)

    def feeds(self) -> list[Feed]:
        return self.store.feeds()

    def feed(self, feed_id: int) -> Feed | None:
        try:
            return self.store.feed(feed_id)
        except LookupError:
            return None

    def update_feed(self, feed_id: int, url: str, title: str | None = None) -> Feed:
        return self.store.update_feed(feed_id, url, title)

    def delete_feed(self, feed_id: int) -> None:
        with self.store.processing_lock() as acquired:
            if not acquired:
                raise ProcessingBusyError("processing already running; retry later")
            episodes = self.store.delete_feed(feed_id)
            for episode in episodes:
                self._remove_episode_files(episode)

    def prune_cache(self) -> int:
        """Remove stale model caches and return the number of files removed."""
        with self.store.processing_lock() as acquired:
            if not acquired:
                raise ProcessingBusyError("processing already running; retry later")
            episodes = self.store.episodes()
            active_keys = {self.cfg.episode_key(episode.guid) for episode in episodes}
            current_paths = {
                self.cfg.raw_path(episode.guid, kind).resolve()
                for episode in episodes
                for kind in ("asr", "diar", "embed")
            }
            if not self.cfg.raw_dir.exists():
                return 0

            removed = 0
            for path in self.cfg.raw_dir.iterdir():
                match = _CACHE_FILE.fullmatch(path.name)
                if not path.is_file() or match is None:
                    continue
                if match.group("episode_key") in active_keys and path.resolve() in current_paths:
                    continue
                self._unlink(path)
                if not path.exists():
                    removed += 1
            return removed

    def sync(self) -> int:
        """Poll every feed and record new episodes. Returns how many were new."""
        return sync_feeds(self.store, self.log)

    def process(
        self,
        limit: int | None = None,
        *,
        glossary: list[str] | None = None,
        retry_errors: bool = False,
        force: bool = False,
    ) -> list[Episode]:
        """Download, transcribe, diarize and merge pending episodes, newest first.

        `glossary` is a list of terms (player names, say) to respell misheard
        names to. None falls back to the glossary file, if there is one; an empty
        list turns the glossary off. A second process that tries to run at the
        same time returns no work immediately. Returns the processed episodes as
        they now stand.
        """
        with self.store.processing_lock() as acquired:
            if not acquired:
                self.log("processing already running; skipping")
                return []
            todo = self.store.pending(limit=limit, retry_errors=retry_errors)
            if not todo:
                return []
            self.log(f"processing {len(todo)} episode(s)")
            proc = Processor(self.cfg, self.log, self._terms(glossary))
            for ep in todo:
                proc.process(self.store, ep, force=force)
            return [self.store.episode(ep.guid) for ep in todo]

    def remerge(self, ref: str | int, *, glossary: list[str] | None = None) -> Episode:
        """Redo the glossary and merge stages from cached model output. Cheap, so
        run it after changing the glossary. Bumps the episode's revision."""
        ep = self._episode(ref)
        remerge(self.store, self.cfg, ep, self._terms(glossary), self.log)
        return self.store.episode(ep.guid)

    # ---- read ------------------------------------------------------------

    def episode(self, ref: str | int) -> Episode | None:
        if isinstance(ref, int):
            return self.store.episode_by_id(ref)
        return self.store.episode(ref)

    def episodes(
        self,
        *,
        status: str | None = None,
        updated_since: str | None = None,
        after: int = 0,
        limit: int | None = None,
    ) -> list[Episode]:
        """Episodes in id order, optionally after a local id and up to a limit.

        `updated_since` is an inclusive ISO 8601 timestamp, so a poller should
        expect repeats and compare `revision`. `after` is a cursor for paging.
        """
        return self.store.episodes(
            status=status, updated_since=updated_since, after_id=after, limit=limit
        )

    def transcript(self, ref: str | int) -> Transcript | None:
        with self.store.snapshot():  # episode, turns and corrections from one revision
            ep = self.episode(ref)
            if ep is None:
                return None
            return Transcript(
                ep,
                self.store.turns(ep.id),
                self.store.corrections(ep.id),
                self.store.speaker_names(ep.guid),
            )

    def speaker_names(self, ref: str | int) -> list[SpeakerName]:
        return self.store.speaker_names(self._episode(ref).guid)

    def speaker_embeddings(self, ref: str | int) -> list[SpeakerEmbedding]:
        """Return locally stored per-episode voice samples, when available."""
        return self.store.speaker_embeddings(self._episode(ref).guid)

    def speaker_matches(self, ref: str | int, min_score: float = 0.70) -> list[SpeakerMatch]:
        """Suggest names from manually enrolled samples in the same feed."""
        if not 0 <= min_score <= 1:
            raise ValueError("min_score must be between 0 and 1")
        return self.store.speaker_matches(self._episode(ref).guid, min_score)

    def set_speaker_name(self, ref: str | int, speaker: str, name: str) -> SpeakerName:
        ep = self._episode(ref)
        speaker = speaker.strip()
        name = name.strip()
        if not speaker:
            raise ValueError("speaker is required")
        if not name:
            raise ValueError("name is required")
        if len(speaker) > 200 or len(name) > 200:
            raise ValueError("speaker and name must be 200 characters or fewer")
        return self.store.set_speaker_name(ep.guid, speaker, name)

    def delete_speaker_name(self, ref: str | int, speaker: str) -> None:
        speaker = speaker.strip()
        if not speaker:
            raise ValueError("speaker is required")
        if len(speaker) > 200:
            raise ValueError("speaker must be 200 characters or fewer")
        self.store.delete_speaker_name(self._episode(ref).guid, speaker)

    def search(self, query: str, limit: int = 20) -> list[Hit]:
        return self.store.search(query, limit)

    def stats(self) -> dict[str, int]:
        return self.store.stats()

    def change_seq(self) -> int:
        return self.store.highest_change_seq()

    def changes(self, after: int = 0, limit: int = 100) -> list[Change]:
        pruned_through = self.store.pruned_through()
        if after < pruned_through:
            raise ChangeLogPrunedError(pruned_through)
        return self.store.changes(after, limit)

    def prune_changes(self, through: int) -> int:
        """Delete change events through a sequence all consumers have acknowledged."""
        return self.store.prune_changes(through)

    def emit_current(self) -> int:
        """Queue the current revision of every completed episode for bootstrap."""
        return self.store.emit_current()

    # ---- internals -------------------------------------------------------

    def _episode(self, ref: str | int) -> Episode:
        ep = self.episode(ref)
        if ep is None:
            raise LookupError(f"no episode {ref}")
        return ep

    def _terms(self, glossary: list[str] | None) -> list[str]:
        return glossary if glossary is not None else load_glossary(self.cfg.glossary_path)

    def _remove_episode_files(self, episode: Episode) -> None:
        audio_root = self.cfg.audio_dir.resolve()
        if episode.audio_path:
            source = Path(episode.audio_path)
            try:
                source.resolve().relative_to(audio_root)
            except ValueError:
                source = None
            if source is not None and not self.store.audio_path_in_use(episode.audio_path):
                self._unlink(source)
                self._unlink(source.with_name(source.stem + ".16k.wav"))

        cache_key = self.cfg.episode_key(episode.guid)
        for path in self.cfg.raw_dir.glob(f"{cache_key}.*"):
            if path.is_file():
                self._unlink(path)

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"  ! could not remove {path.name}: {exc}")
