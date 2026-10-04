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
import traceback
from pathlib import Path

from .audio import wav16k_path
from .config import Config, default_config
from .deepinfra import DeepInfraError, guest_hints as deepinfra_guest_hints
from .glossary import load as load_glossary
from .models import (
    Change,
    Episode,
    EpisodeDetail,
    Feed,
    Hit,
    SpeakerEmbedding,
    SpeakerMatch,
    SpeakerName,
    SpeakerProfile,
    Transcript,
)
from .identity import guest_hints as extract_guest_hints
from .pipeline import (
    Log,
    Processor,
    apply_work_result,
    backfill_feed,
    delete_audio,
    parse_work_result,
    prepare_audio,
    raw_output_path,
    remerge,
    sync_feeds,
)
from .store import STATUS_DOWNLOADED, STATUS_ERROR, Store


_CACHE_FILE = re.compile(r"^(?P<episode_key>[0-9a-f]{12})\.(?:asr|diar|embed)-[0-9a-f]{12}\.json$")


class ProcessingBusyError(RuntimeError):
    """A destructive operation was attempted during model processing."""


class LeaseError(RuntimeError):
    """A worker's lease is not the episode's current one: it was re-claimed
    after expiring, or its result was already applied."""


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
                path.resolve()
                for episode in episodes
                for kind in ("asr", "diar", "embed")
                for path in (
                    self.cfg.raw_path(episode.guid, kind),
                    raw_output_path(self.store, self.cfg, episode, kind),
                )
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

    def backfill(self, feed_id: int, count: int) -> int:
        """Record a feed's `count` newest entries, including older ones a normal
        sync skips. Returns how many were new."""
        return backfill_feed(self.store, feed_id, count, self.log)

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
                self.store.rebuild_speaker_profiles(ep.feed_id, changed_guid=ep.guid)
            return [self.store.episode(ep.guid) for ep in todo]

    # ---- remote workers (docs/worker-contract.md) --------------------------

    def prepare(self, limit: int | None = None, *, retry_errors: bool = False) -> list[Episode]:
        """Download and normalize pending episodes so workers can claim them.
        Runs no models. Returns the episodes that are now ready."""
        ready = []
        for ep in self.store.pending(limit=limit, retry_errors=retry_errors):
            self.log(f"[{ep.id}] {ep.title or ep.guid}")
            try:
                prepare_audio(self.store, self.cfg, ep, self.log)
            except Exception as exc:  # noqa: BLE001 - one bad download shouldn't stop the rest
                self.log(f"  ! failed: {exc}")
                self.store.set_status(ep.id, STATUS_ERROR, error=traceback.format_exc(limit=3))
                continue
            if ep.status == STATUS_ERROR:  # a retried failure goes back in the queue
                self.store.set_status(ep.id, STATUS_DOWNLOADED)
            ready.append(self.store.episode(ep.guid))
        return ready

    def claim_work(self, worker: str) -> tuple[Episode, str, str] | None:
        """Lease the next prepared episode to a worker: (episode, lease_id, expires)."""
        return self.store.claim(worker, self.cfg.lease_seconds)

    def work_audio(self, guid: str, lease_id: str) -> Path:
        """The leased episode's 16 kHz mono WAV."""
        ep = self.store.lease_holder(guid, lease_id)
        if ep is None:
            raise LeaseError("lease is not current")
        return prepare_audio(self.store, self.cfg, ep, self.log)

    def submit_work(self, guid: str, body: dict, *, glossary: list[str] | None = None) -> Episode:
        """Apply a worker's result (validated first, then at most once per lease)
        and return the episode as it now stands."""
        result = parse_work_result(body)
        lease_id = body.get("lease_id")
        ep = self.store.consume_lease(guid, lease_id) if isinstance(lease_id, str) else None
        if ep is None:
            raise LeaseError("lease is not current")
        self.log(f"[{ep.id}] {ep.title or ep.guid}: worker result")
        try:
            apply_work_result(self.store, self.cfg, ep, result, self._terms(glossary), self.log)
        except Exception:
            self.store.set_status(ep.id, STATUS_ERROR, error=traceback.format_exc(limit=3))
            raise
        self.store.rebuild_speaker_profiles(ep.feed_id, changed_guid=ep.guid)
        if not self.cfg.keep_audio and ep.audio_path:
            try:
                delete_audio(self.store, ep, wav16k_path(Path(ep.audio_path), self.cfg.audio_dir))
            except OSError as exc:
                self.log(f"  ! could not delete audio: {exc}")
        return self.store.episode(guid)

    def fail_work(self, guid: str, lease_id: str, error: str) -> None:
        """Record a worker's failure and release its lease."""
        ep = self.store.consume_lease(guid, lease_id)
        if ep is None:
            raise LeaseError("lease is not current")
        self.store.set_status(ep.id, STATUS_ERROR, error=f"worker: {error}"[:2000])

    def remerge(self, ref: str | int, *, glossary: list[str] | None = None) -> Episode:
        """Redo the glossary and merge stages from cached model output. Cheap, so
        run it after changing the glossary. Bumps the episode's revision."""
        ep = self._episode(ref)
        remerge(self.store, self.cfg, ep, self._terms(glossary), self.log)
        self.store.rebuild_speaker_profiles(ep.feed_id, changed_guid=ep.guid)
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
            return self._transcript(ref)

    def episode_detail(self, ref: str | int) -> EpisodeDetail | None:
        """Read all fields used by the HTTP detail response from one DB snapshot."""
        with self.store.snapshot():
            transcript = self._transcript(ref)
            if transcript is None:
                return None
            episode = transcript.episode
            profiles = self.speaker_profiles(episode.feed_id)
            host_name = self._host_name(episode.feed_id, profiles)
            return EpisodeDetail(
                transcript=transcript,
                feed=self.store.feed(episode.feed_id),
                speaker_matches=self.store.speaker_matches(episode.guid),
                guest_hints=self._guest_hints(episode, transcript, host_name, use_llm=False),
            )

    def speaker_names(self, ref: str | int) -> list[SpeakerName]:
        return self.store.speaker_names(self._episode(ref).guid)

    def speaker_embeddings(self, ref: str | int) -> list[SpeakerEmbedding]:
        """Return locally stored per-episode voice samples, when available."""
        return self.store.speaker_embeddings(self._episode(ref).guid)

    def speaker_profiles(self, feed_id: int) -> list[SpeakerProfile]:
        """Return show-level identity profiles built from manual speaker labels."""
        return self.store.speaker_profiles(feed_id)

    def rebuild_speaker_profiles(self, feed_id: int) -> list[SpeakerProfile]:
        """Rebuild show-level normalized voice centroids from manual labels."""
        if self.feed(feed_id) is None:
            raise LookupError(f"no feed {feed_id}")
        return self.store.rebuild_speaker_profiles(feed_id)

    def speaker_matches(self, ref: str | int, min_score: float = 0.70) -> list[SpeakerMatch]:
        """Suggest names from the show's manually enrolled voice profiles."""
        if not 0 <= min_score <= 1:
            raise ValueError("min_score must be between 0 and 1")
        return self.store.speaker_matches(self._episode(ref).guid, min_score)

    def guest_hints(self, ref: str | int, *, use_llm: bool = False):
        """Extract conservative guest-name hints from episode context.

        This is text evidence, not a voice identification result. The most
        recurrent show-level profile is treated as the host and filtered from
        the hints.
        """
        with self.store.snapshot():
            episode = self._episode(ref)
            transcript = self._transcript(episode.guid)
            profiles = self.speaker_profiles(episode.feed_id)
            host_name = self._host_name(episode.feed_id, profiles)
        return self._guest_hints(episode, transcript, host_name, use_llm=use_llm)

    def _host_name(self, feed_id: int, profiles: list[SpeakerProfile]) -> str | None:
        if profiles:
            return max(profiles, key=lambda profile: profile.sample_count).name
        return self.store.most_common_manual_speaker_name(feed_id)

    def _guest_hints(
        self,
        episode: Episode,
        transcript: Transcript | None,
        host_name: str | None,
        *,
        use_llm: bool,
    ):
        hints = extract_guest_hints(
            episode,
            transcript.turns if transcript else (),
            host_name=host_name,
        )
        if use_llm and self.cfg.deepinfra_token:
            try:
                hints.extend(
                    deepinfra_guest_hints(
                        episode,
                        transcript.turns if transcript else (),
                        token=self.cfg.deepinfra_token,
                        model=self.cfg.deepinfra_model,
                        base_url=self.cfg.deepinfra_base_url,
                        host_name=host_name,
                        existing_names={hint.name for hint in hints},
                    )
                )
            except DeepInfraError as exc:
                self.log(f"  ! DeepInfra guest extraction skipped: {exc}")
        return hints

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
        mapping, changed = self.store._set_speaker_name(ep.guid, speaker, name)
        self.store.rebuild_speaker_profiles(
            ep.feed_id, changed_guid=ep.guid, notify_changes=False
        )
        if changed:
            self.store.invalidate_feed_metadata(ep.feed_id, changed_guid=ep.guid)
        return mapping

    def delete_speaker_name(self, ref: str | int, speaker: str) -> None:
        speaker = speaker.strip()
        if not speaker:
            raise ValueError("speaker is required")
        if len(speaker) > 200:
            raise ValueError("speaker must be 200 characters or fewer")
        ep = self._episode(ref)
        changed = self.store.delete_speaker_name(ep.guid, speaker)
        self.store.rebuild_speaker_profiles(
            ep.feed_id, changed_guid=ep.guid, notify_changes=False
        )
        if changed:
            self.store.invalidate_feed_metadata(ep.feed_id, changed_guid=ep.guid)

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

    def _transcript(self, ref: str | int) -> Transcript | None:
        ep = self.episode(ref)
        if ep is None:
            return None
        return Transcript(
            ep,
            self.store.turns(ep.id),
            self.store.corrections(ep.id),
            self.store.speaker_names(ep.guid),
        )

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
