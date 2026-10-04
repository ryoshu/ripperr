import hashlib
import json
import re
import sqlite3
import tomllib
from pathlib import Path

import pytest

from ripperr import pipeline
from ripperr.api import ChangeLogPrunedError, ProcessingBusyError, Ripperr
from ripperr.config import Config
from ripperr.models import Turn
from ripperr.pipeline import Processor, _read_cache, _write_cache
from ripperr.store import Store, public_guid


def make(tmp_path):
    """A Ripperr with one episode whose model output is already cached, so
    remerge runs without any model, network or audio. Returns (rip, guid)."""
    cfg = Config(root=tmp_path)
    rip = Ripperr(cfg, log=lambda _: None)
    fid = rip.add_feed("http://feed").id
    rip.store.add_episode(fid, "ep-1", "Ep 1", None, "http://a")
    guid = rip.episodes()[0].guid
    words = [
        {"start": 0, "end": 0.4, "word": "Basial"}, {"start": 0.4, "end": 0.9, "word": "Tootin"},
        {"start": 1, "end": 1.5, "word": "runs"},
    ]
    cfg.raw_path(guid, "asr").write_text(json.dumps({"segments": [{"words": words}]}))
    cfg.raw_path(guid, "diar").write_text(json.dumps([{"start": 0, "end": 2, "speaker": "SPEAKER_01"}]))
    return rip, guid


# ---- the API surface -------------------------------------------------------


def test_remerge_applies_glossary_and_bumps_revision(tmp_path):
    rip, guid = make(tmp_path)
    assert rip.episode(guid).revision == 0

    ep = rip.remerge(guid, glossary=["Bhayshul Tuten"])
    assert (ep.status, ep.revision) == ("done", 1)
    tr = rip.transcript(guid)
    assert [t.text for t in tr.turns] == ["Bhayshul Tuten runs"]
    assert [(c.heard, c.fixed, c.count) for c in tr.corrections] == [("Basial Tootin", "Bhayshul Tuten", 1)]

    # a second remerge without a glossary rewrites the transcript: new revision, no corrections
    ep = rip.remerge(guid, glossary=[])
    assert ep.revision == 2
    tr = rip.transcript(guid)
    assert tr.turns[0].text == "Basial Tootin runs" and tr.corrections == []


def test_glossary_file_is_the_default(tmp_path):
    rip, guid = make(tmp_path)
    rip.cfg.glossary_path.write_text("# players\nBhayshul Tuten\n\n")
    rip.remerge(guid)
    assert rip.transcript(guid).turns[0].text == "Bhayshul Tuten runs"


def test_guid_and_local_id_both_address_an_episode(tmp_path):
    rip, guid = make(tmp_path)
    ep = rip.episode(guid)
    assert (ep.source_guid, ep.guid) == ("ep-1", public_guid("http://feed", "ep-1"))
    assert rip.episode(ep.id).guid == guid
    assert rip.episode("nope") is None and rip.transcript("nope") is None


def test_speaker_names_are_episode_scoped_and_leave_turns_raw(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    rip.remerge(guid, glossary=[])
    assert rip.speaker_names(guid) == []

    before = rip.episode(guid)
    monkeypatch.setattr("ripperr.store._now", lambda: "2026-09-21T12:00:00+00:00")
    mapping = rip.set_speaker_name(guid, "SPEAKER_01", "Matt Harmon")
    assert (mapping.speaker, mapping.name, mapping.method) == (
        "SPEAKER_01", "Matt Harmon", "manual"
    )
    after = rip.episode(guid)
    assert after.updated_at == "2026-09-21T12:00:00+00:00"
    assert after.revision == before.revision
    assert rip.changes()[-1].revision == before.revision
    transcript = rip.transcript(guid)
    assert transcript.turns[0].speaker == "SPEAKER_01"
    assert transcript.speaker_names == [mapping]

    rip.delete_speaker_name(guid, "SPEAKER_01")
    assert rip.speaker_names(guid) == []
    rip.close()


def test_speaker_names_require_a_current_turn_label(tmp_path):
    rip, guid = make(tmp_path)
    with pytest.raises(ValueError, match="unknown speaker"):
        rip.set_speaker_name(guid, "SPEAKER_99", "Nobody")
    rip.close()


def test_speaker_embeddings_round_trip(tmp_path):
    rip, guid = make(tmp_path)
    rip.store.replace_turns(
        1,
        [Turn(0, "SPEAKER_01", 0, 1, "hello")],
        speaker_embeddings={"SPEAKER_01": [0.1, -0.2, 0.3]},
    )

    [sample] = rip.speaker_embeddings(guid)
    assert sample.speaker == "SPEAKER_01"
    assert sample.embedding == (0.1, -0.2, 0.3)
    rip.close()


def test_speaker_matches_use_named_samples_from_the_same_feed(tmp_path):
    rip, guid = make(tmp_path)
    feed = rip.feeds()[0]
    rip.store.add_episode(feed.id, "ep-2", "Ep 2", None, "http://a-2")
    other_guid = rip.episodes()[-1].guid
    rip.store.replace_turns(
        1,
        [Turn(0, "SPEAKER_01", 0, 1, "hello")],
        speaker_embeddings={"SPEAKER_01": [1.0, 0.0]},
    )
    rip.set_speaker_name(guid, "SPEAKER_01", "Host")
    rip.store.replace_turns(
        2,
        [Turn(0, "SPEAKER_00", 0, 1, "hello")],
        speaker_embeddings={"SPEAKER_00": [0.99, 0.01]},
    )
    other_feed = rip.add_feed("http://other-feed")
    rip.store.add_episode(other_feed.id, "other-1", "Other", None, "http://other-audio")
    rip.store.replace_turns(
        3,
        [Turn(0, "SPEAKER_00", 0, 1, "hello")],
        speaker_embeddings={"SPEAKER_00": [0.99, 0.01]},
    )
    rip.set_speaker_name(3, "SPEAKER_00", "Other Host")

    [match] = rip.speaker_matches(other_guid, min_score=0.9)
    assert (match.speaker, match.name, match.sample_count) == ("SPEAKER_00", "Host", 1)
    assert rip.speaker_names(other_guid) == []
    rip.close()


def test_diarization_recompute_clears_speaker_names(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    rip.remerge(guid, glossary=[])
    rip.set_speaker_name(guid, "SPEAKER_01", "Host")
    rip.store.set_status(1, "new")
    rip.cfg.raw_path(guid, "diar").unlink()

    proc = Processor(rip.cfg, lambda _: None)
    proc._diarizer = type(
        "D", (), {"run": lambda self, wav: [{"start": 0, "end": 2, "speaker": "SPEAKER_00"}]}
    )()
    monkeypatch.setattr(proc, "_ensure_audio", lambda store, episode: Path("x.wav"))
    proc.process(rip.store, rip.episode(guid))

    assert rip.speaker_names(guid) == []
    assert rip.transcript(guid).turns[0].speaker == "SPEAKER_00"
    rip.close()


def test_failed_merge_then_retry_uses_new_diarization_key(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    rip.remerge(guid, glossary=[])
    rip.set_speaker_name(guid, "SPEAKER_01", "Host")
    rip.store.set_status(1, "new")
    rip.cfg.raw_path(guid, "diar").unlink()

    proc = Processor(rip.cfg, lambda _: None)
    proc._diarizer = type(
        "D", (), {"run": lambda self, wav: [{"start": 0, "end": 2, "speaker": "SPEAKER_00"}]}
    )()
    monkeypatch.setattr(proc, "_ensure_audio", lambda store, episode: Path("x.wav"))
    monkeypatch.setattr(pipeline, "merge_and_save", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    proc.process(rip.store, rip.episode(guid))
    assert rip.speaker_names(guid)[0].name == "Host"

    monkeypatch.undo()
    proc = Processor(rip.cfg, lambda _: None)
    monkeypatch.setattr(proc, "_ensure_audio", lambda store, episode: Path("x.wav"))
    proc.process(rip.store, rip.episode(guid))
    assert rip.speaker_names(guid) == []
    rip.close()


def test_replacing_turns_prunes_names_for_missing_labels(tmp_path):
    rip, guid = make(tmp_path)
    rip.remerge(guid, glossary=[])
    rip.set_speaker_name(guid, "SPEAKER_01", "Host")
    rip.store.replace_turns(1, [Turn(0, "SPEAKER_00", 0, 1, "hello")])
    assert rip.speaker_names(guid) == []
    rip.close()


def test_episodes_filters_by_status_and_update_time(tmp_path):
    rip, guid = make(tmp_path)
    assert rip.episodes(status="done") == []
    rip.remerge(guid, glossary=[])
    done = rip.episodes(status="done")
    assert [e.guid for e in done] == [guid]
    assert rip.episodes(updated_since=done[0].updated_at) == done
    assert rip.episodes(updated_since="2999-01-01T00:00:00+00:00") == []


def test_emit_current_is_available_through_public_api(tmp_path):
    rip, guid = make(tmp_path)
    rip.remerge(guid, glossary=[])
    assert rip.emit_current() == 1
    assert rip.changes()[-1].kind == "transcript"
    rip.close()


def test_prune_changes_requires_acknowledged_cursor_and_keeps_sequence_monotonic(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    feed = rip.add_feed("http://feed")
    rip.store.add_episode(feed.id, "one", "One", None, "http://audio/one")
    assert rip.change_seq() == 1

    assert rip.prune_changes(1) == 1
    rip.close()
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    with pytest.raises(ChangeLogPrunedError) as exc:
        rip.changes()
    assert exc.value.pruned_through == 1
    assert rip.changes(after=1) == []
    assert rip.change_seq() == 1

    rip.store.add_episode(feed.id, "two", "Two", None, "http://audio/two")
    assert rip.change_seq() == 2
    assert rip.changes(after=1)[0].seq == 2
    with pytest.raises(ValueError):
        rip.prune_changes(-1)
    rip.close()


def test_public_feed_management_and_episode_pagination(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    feed = rip.add_feed("http://feed", "Old")
    assert rip.feed(feed.id) == feed
    assert rip.update_feed(feed.id, "http://new-feed", "New").title == "New"

    for source_guid in ("one", "two", "three"):
        rip.store.add_episode(feed.id, source_guid, source_guid, None, "http://audio")

    first = rip.episodes(limit=2)
    assert [episode.source_guid for episode in first] == ["one", "two"]
    assert [episode.source_guid for episode in rip.episodes(after=first[-1].id)] == ["three"]
    assert rip.feed(feed.id).url == "http://new-feed"
    rip.delete_feed(feed.id)
    assert rip.feed(feed.id) is None
    rip.close()


def test_search_returns_typed_hits_with_guid(tmp_path):
    rip, guid = make(tmp_path)
    rip.remerge(guid, glossary=["Bhayshul Tuten"])
    (hit,) = rip.search("Tuten")
    assert hit.guid == guid and "[Tuten]" in hit.snippet


def test_rss_published_is_iso_so_pending_is_newest_first(tmp_path):
    from ripperr.feeds import parse_feed

    def item(guid, date):
        return (f'<item><guid>{guid}</guid><title>{guid}</title><description>Summary {guid}</description>'
                f'<pubDate>{date}</pubDate>'
                f'<enclosure url="http://a/{guid}.mp3" type="audio/mpeg"/></item>')

    xml = ('<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
           + item("older", "Wed, 01 Jan 2025 08:00:00 GMT")   # "Wed" sorts after "Mon" as text
           + item("newer", "Mon, 06 Jan 2025 08:00:00 GMT")
           + "</channel></rss>")
    _, episodes = parse_feed(xml)
    assert {e["source_guid"]: e["summary"] for e in episodes} == {
        "older": "Summary older", "newer": "Summary newer"}
    assert {e["source_guid"]: e["published"] for e in episodes} == {
        "older": "2025-01-01T08:00:00+00:00", "newer": "2025-01-06T08:00:00+00:00"}

    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    fid = rip.add_feed("http://feed").id
    for e in episodes:
        rip.store.add_episode(fid, e["source_guid"], e["title"], e["published"], e["audio_url"])
    assert [e.source_guid for e in rip.store.pending()] == ["newer", "older"]


# ---- episode identity ------------------------------------------------------


def test_same_source_guid_in_two_feeds_stays_distinct(tmp_path):
    store = Store(tmp_path / "t.db")
    a, b = store.add_feed("http://a").id, store.add_feed("http://b").id
    assert store.add_episode(a, "1", "A1", None, "http://a/1.mp3")
    assert store.add_episode(b, "1", "B1", None, "http://b/1.mp3")
    eps = store.episodes()
    assert [e.title for e in eps] == ["A1", "B1"]
    assert len({e.guid for e in eps}) == 2 and {e.source_guid for e in eps} == {"1"}


def test_processing_lock_is_exclusive_and_released(tmp_path):
    first = Store(tmp_path / "t.db")
    second = Store(tmp_path / "t.db")

    with first.processing_lock() as acquired:
        assert acquired
        with second.processing_lock() as acquired:
            assert not acquired

    with second.processing_lock() as acquired:
        assert acquired

    first.close()
    second.close()


def test_old_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(
        """CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT, added_at TEXT NOT NULL);
           CREATE TABLE episodes (id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, guid TEXT NOT NULL UNIQUE,
             title TEXT, published TEXT, audio_url TEXT NOT NULL, audio_path TEXT, duration REAL,
             status TEXT NOT NULL DEFAULT 'new', error TEXT, updated_at TEXT NOT NULL);
           INSERT INTO feeds VALUES (1, 'u', NULL, 't');
           INSERT INTO episodes (feed_id, guid, audio_url, updated_at) VALUES (1, 'g', 'a', 't');"""
    )
    c.commit()
    c.close()
    store = Store(db)
    ep = store.episode("g")  # existing episodes keep their public guid
    assert (ep.revision, ep.merged_at, ep.source_guid) == (0, None, "g")
    assert store.add_episode(1, "g", "T", None, "a") is False  # not duplicated on the next sync
    assert store.add_episode(1, "new", "N", None, "b") is True
    assert store.episode(public_guid("u", "new")).source_guid == "new"
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 9


def test_current_schema_skips_migration_on_reopen(tmp_path, monkeypatch):
    db = tmp_path / "current.db"
    Store(db).close()

    def unexpected_migration(self):
        raise AssertionError("current databases should not migrate on every open")

    monkeypatch.setattr(Store, "_migrate", unexpected_migration)
    Store(db).close()


def test_schema_migrates_old_change_constraint(tmp_path):
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_guid TEXT NOT NULL,
            revision INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('transcript', 'metadata')),
            occurred_at TEXT NOT NULL
        );
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()

    store = Store(db)
    Store._insert_change(store.conn, "episode", 1, "deleted")
    assert store.changes()[0].kind == "deleted"
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 9
    store.close()


def test_v2_deleted_events_seed_tombstones(tmp_path):
    db = tmp_path / "v2.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_guid TEXT NOT NULL,
            revision INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('transcript', 'metadata', 'deleted')),
            occurred_at TEXT NOT NULL
        );
        INSERT INTO changes VALUES (7, 'episode', 3, 'deleted', 't');
        PRAGMA user_version = 2;
        """
    )
    conn.commit()
    conn.close()

    store = Store(db)
    assert store.conn.execute(
        "SELECT revision FROM episode_tombstones WHERE guid = 'episode'"
    ).fetchone()[0] == 3
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 9
    store.close()


def test_delete_feed_emits_events_and_removes_owned_files(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    feed = rip.add_feed("http://feed")
    rip.store.add_episode(feed.id, "one", "One", None, "http://audio")
    episode = rip.episodes()[0]
    source = rip.cfg.audio_dir / "one.mp3"
    wav = rip.cfg.audio_dir / "one.16k.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"audio")
    wav.write_bytes(b"wav")
    rip.store.set_status(episode.id, "downloaded", audio_path=str(source))
    cache_key = hashlib.sha1(episode.guid.encode()).hexdigest()[:12]
    old_cache = rip.cfg.raw_dir / f"{cache_key}.old.json"
    old_cache.parent.mkdir(parents=True, exist_ok=True)
    old_cache.write_text("{}")

    rip.delete_feed(feed.id)

    assert not source.exists() and not wav.exists() and not old_cache.exists()
    assert rip.changes()[-1].kind == "deleted"
    rip.close()


def test_delete_feed_keeps_audio_shared_by_another_episode(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    first = rip.add_feed("http://first")
    second = rip.add_feed("http://second")
    rip.store.add_episode(first.id, "one", "One", None, "http://audio")
    rip.store.add_episode(second.id, "two", "Two", None, "http://audio")
    episodes = {episode.source_guid: episode for episode in rip.episodes()}
    source = rip.cfg.audio_dir / "shared.mp3"
    wav = rip.cfg.audio_dir / "shared.16k.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"audio")
    wav.write_bytes(b"wav")
    for episode in episodes.values():
        rip.store.set_status(episode.id, "downloaded", audio_path=str(source))

    rip.delete_feed(first.id)
    assert source.exists() and wav.exists()

    rip.delete_feed(second.id)
    assert not source.exists() and not wav.exists()
    rip.close()


def test_delete_feed_waits_for_processing_lock(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    feed = rip.add_feed("http://feed")

    with rip.store.processing_lock() as acquired:
        assert acquired
        with pytest.raises(ProcessingBusyError):
            rip.delete_feed(feed.id)

    assert rip.feed(feed.id) == feed
    rip.close()


def test_prune_cache_removes_stale_and_orphaned_variants(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    feed = rip.add_feed("http://feed")
    rip.store.add_episode(feed.id, "one", "One", None, "http://audio")
    episode = rip.episodes()[0]
    current = rip.cfg.raw_path(episode.guid, "asr")
    stale = rip.cfg.raw_dir / f"{rip.cfg.episode_key(episode.guid)}.asr-deadbeefdead.json"
    orphan = rip.cfg.raw_dir / "0123456789ab.diar-deadbeefdead.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text("{}")
    stale.write_text("{}")
    orphan.write_text("{}")

    assert rip.prune_cache() == 2
    assert current.exists() and not stale.exists() and not orphan.exists()
    rip.close()


def test_readding_deleted_episode_keeps_revision_monotonic(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    feed = rip.add_feed("http://feed")
    rip.store.add_episode(feed.id, "one", "One", None, "http://audio")
    episode = rip.episodes()[0]
    rip.store.replace_turns(episode.id, [Turn(0, "S", 0, 1, "hello")])
    guid = episode.guid

    rip.delete_feed(feed.id)
    readded_feed = rip.add_feed("http://feed")
    rip.store.add_episode(readded_feed.id, "one", "One again", None, "http://audio")

    readded = rip.episode(guid)
    assert readded.revision == 2
    assert [event.revision for event in rip.changes()] == [0, 1, 1, 2]
    rip.close()


# ---- feed refresh ----------------------------------------------------------


def test_resync_refreshes_changed_metadata_but_keeps_state(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    fid = store.add_feed("http://f").id
    monkeypatch.setattr("ripperr.store._now", lambda: "2030-01-01T00:00:00+00:00")
    assert store.add_episode(fid, "g", "Old", "2025-01-01T00:00:00+00:00", "http://expired") is True
    store.set_status(1, "error", error="boom", audio_path="/x")

    monkeypatch.setattr("ripperr.store._now", lambda: "2030-06-01T00:00:00+00:00")
    assert store.add_episode(fid, "g", "Old", "2025-01-01T00:00:00+00:00", "http://expired") is False
    assert store.episode_by_id(1).updated_at == "2030-01-01T00:00:00+00:00"  # nothing changed

    monkeypatch.setattr("ripperr.store._now", lambda: "2030-09-01T00:00:00+00:00")
    assert store.add_episode(fid, "g", "New", "2025-02-02T00:00:00+00:00", "http://fixed") is False
    ep = store.episode_by_id(1)
    assert (ep.title, ep.published, ep.audio_url) == ("New", "2025-02-02T00:00:00+00:00", "http://fixed")
    assert (ep.status, ep.error, ep.audio_path, ep.revision) == ("error", "boom", "/x", 0)
    assert ep.updated_at == "2030-09-01T00:00:00+00:00"
    assert store.pending(retry_errors=True)[0].audio_url == "http://fixed"  # the retry uses it

    store.add_episode(fid, "g", "New", None, "http://fixed")  # feed dropped the date
    assert store.episode_by_id(1).published == "2025-02-02T00:00:00+00:00"
    store.add_episode(fid, "g", "New", None, "http://fixed", summary="A useful summary")
    assert store.episode_by_id(1).summary == "A useful summary"


def test_new_feed_starts_at_latest_and_follows_new_entries(tmp_path, monkeypatch):
    episodes = [
        {"source_guid": "newest", "title": "Newest", "published": None, "audio_url": "a"},
        {"source_guid": "older", "title": "Older", "published": None, "audio_url": "b"},
    ]
    monkeypatch.setattr("ripperr.feeds.parse_feed", lambda _, _known=None, _refresh=None: ("Show", episodes))

    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    rip.add_feed("http://feed")
    assert rip.sync() == 1
    assert [episode.source_guid for episode in rip.episodes()] == ["newest"]

    assert rip.sync() == 0
    episodes.insert(0, {"source_guid": "newer", "title": "Newer", "published": None, "audio_url": "c"})
    assert rip.sync() == 1
    assert [episode.source_guid for episode in rip.episodes()] == ["newest", "newer"]


def test_sync_does_not_backfill_entries_after_first_known_guid(tmp_path, monkeypatch):
    feeds = [[
        {"source_guid": "newest", "title": "Newest", "published": None, "audio_url": "a"},
    ], [
        {"source_guid": "newer", "title": "Newer", "published": None, "audio_url": "b"},
        {"source_guid": "newest", "title": "Newest", "published": None, "audio_url": "a"},
        {"source_guid": "historical", "title": "Historical", "published": None, "audio_url": "c"},
    ]]
    monkeypatch.setattr("ripperr.feeds.parse_feed", lambda _, _known=None, _refresh=None: ("Show", feeds.pop(0)))

    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    rip.add_feed("http://feed")
    assert rip.sync() == 1
    assert rip.sync() == 1
    assert [episode.source_guid for episode in rip.episodes()] == ["newest", "newer"]


# ---- cache durability ------------------------------------------------------


def test_truncated_caches_are_misses_and_get_rewritten(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    cfg, ep = rip.cfg, rip.episode(guid)
    cfg.raw_path(guid, "asr").write_text('{"segments": [{"wor')  # cut off mid-write
    cfg.raw_path(guid, "diar").write_text("[{")
    monkeypatch.setattr(pipeline.asr, "transcribe", lambda *a, **k: {"segments": []})
    proc = Processor(cfg, lambda _: None)
    proc._diarizer = type("D", (), {"run": lambda self, wav: [{"start": 0, "end": 1, "speaker": "S"}]})()

    assert proc._ensure_asr(ep, Path("x.wav"), force=False) == {"segments": []}
    assert proc._ensure_diarization(ep, Path("x.wav"), force=False) == [{"start": 0, "end": 1, "speaker": "S"}]
    assert _read_cache(cfg.raw_path(guid, "asr")) == {"segments": []}
    assert _read_cache(cfg.raw_path(guid, "diar")) is not None


def test_model_cache_changes_with_backend_configuration(tmp_path):
    guid = "episode"
    asr_a = Config(root=tmp_path, asr_backend="mlx", asr_model="model-a")
    asr_b = Config(root=tmp_path, asr_backend="faster-whisper", asr_model="model-a")
    diar_a = Config(root=tmp_path, diarization_backend="senko")
    diar_b = Config(root=tmp_path, diarization_backend="pyannote")

    assert asr_a.raw_path(guid, "asr") != asr_b.raw_path(guid, "asr")
    assert diar_a.raw_path(guid, "diar") != diar_b.raw_path(guid, "diar")


def test_remerge_with_unreadable_cache_says_so(tmp_path):
    rip, guid = make(tmp_path)
    rip.cfg.raw_path(guid, "diar").write_text("[")
    with pytest.raises(FileNotFoundError, match="usable cached"):
        rip.remerge(guid)


def test_cache_write_failure_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    _write_cache(path, {"a": 1})
    assert _read_cache(path) == {"a": 1}

    with pytest.raises(TypeError):  # not serialisable
        _write_cache(path, {"a": object()})

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.os, "replace", boom)
    with pytest.raises(OSError):
        _write_cache(path, {"a": 2})
    assert _read_cache(path) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


# ---- completion is one transaction ----------------------------------------


def test_replace_turns_marks_done_and_clears_error_in_one_step(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_episode(store.add_feed("http://f").id, "g", "T", None, "http://a")
    store.set_status(1, "error", error="boom")
    store.replace_turns(1, [Turn(0, "A", 0, 1, "hello")])
    ep = store.episode_by_id(1)
    assert (ep.status, ep.error, ep.revision) == ("done", None, 1)


def test_failed_transcript_swap_rolls_back_everything(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_episode(store.add_feed("http://f").id, "g", "T", None, "http://a")
    store.replace_turns(1, [Turn(0, "A", 0, 1, "old text")])
    store.set_status(1, "downloaded")
    before = store.episode_by_id(1)

    with pytest.raises(sqlite3.IntegrityError):  # second turn has no text
        store.replace_turns(1, [Turn(0, "A", 0, 1, "new text"), Turn(1, "A", 1, 2, None)])

    assert store.episode_by_id(1) == before  # revision, status, timestamps untouched
    assert [t.text for t in store.turns(1)] == ["old text"]
    assert store.search("old") and not store.search("new")


def test_cleanup_failure_after_commit_does_not_mark_episode_failed(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    rip.cfg.keep_audio = False
    monkeypatch.setattr(Processor, "_ensure_audio", lambda self, store, ep: Path("x.wav"))

    def boom(self, *a):
        raise PermissionError("nope")

    monkeypatch.setattr(Processor, "_delete_audio", boom)
    Processor(rip.cfg, lambda _: None).process(rip.store, rip.episode(guid))
    ep = rip.episode(guid)
    assert (ep.status, ep.error, ep.revision) == ("done", None, 1)


def test_failure_before_commit_marks_error_and_stores_no_transcript(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    monkeypatch.setattr(Processor, "_ensure_audio", lambda self, store, ep: Path("x.wav"))

    def boom(self, *a):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(Processor, "_ensure_asr", boom)
    Processor(rip.cfg, lambda _: None).process(rip.store, rip.episode(guid))
    ep = rip.episode(guid)
    assert ep.status == "error" and "model exploded" in ep.error
    assert rip.transcript(guid).turns == []


# ---- consistent reads ------------------------------------------------------


def test_transcript_is_one_consistent_snapshot_even_if_another_process_commits(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    rip.store.replace_turns(1, [Turn(0, "A", 0, 1, "old text")])  # revision 1
    other = Store(rip.cfg.db_path)  # stands in for a second process

    real_turns = rip.store.turns

    def racing(episode_id):
        other.replace_turns(1, [Turn(0, "A", 0, 1, "new text")])  # revision 2 lands mid-read
        return real_turns(episode_id)

    monkeypatch.setattr(rip.store, "turns", racing)
    tr = rip.transcript(guid)
    assert tr.episode.revision == 1 and [t.text for t in tr.turns] == ["old text"]

    monkeypatch.undo()
    tr = rip.transcript(guid)  # the next read sees the new revision, all of it
    assert tr.episode.revision == 2 and [t.text for t in tr.turns] == ["new text"]


# ---- audio cleanup ---------------------------------------------------------


def test_audio_deleted_and_path_cleared_when_downloaded_in_the_same_run(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    rip.cfg.keep_audio = False
    src, wav = rip.cfg.audio_dir / "ep.mp3", rip.cfg.audio_dir / "ep.16k.wav"
    monkeypatch.setattr(pipeline.feeds, "download", lambda url, d, title: (src.write_bytes(b"x"), src)[1])
    monkeypatch.setattr(pipeline.audio, "to_wav16k", lambda s, d: (wav.write_bytes(b"y"), wav)[1])
    monkeypatch.setattr(pipeline.audio, "duration_seconds", lambda p: 12.0)

    assert rip.episode(guid).audio_path is None  # so this run has to download it
    Processor(rip.cfg, lambda _: None).process(rip.store, rip.episode(guid))

    ep = rip.episode(guid)
    assert not src.exists() and not wav.exists()
    assert (ep.status, ep.audio_path, ep.duration) == ("done", None, 12.0)


def test_audio_path_kept_when_the_source_could_not_be_deleted(tmp_path, monkeypatch):
    rip, guid = make(tmp_path)
    rip.cfg.keep_audio = False
    stuck = tmp_path / "stuck"
    stuck.mkdir()  # unlink() on a directory fails, standing in for any deletion error
    rip.store.set_status(1, "downloaded", audio_path=str(stuck))
    monkeypatch.setattr(Processor, "_ensure_audio", lambda self, store, ep: tmp_path / "ep.16k.wav")

    Processor(rip.cfg, lambda _: None).process(rip.store, rip.episode(guid))
    ep = rip.episode(guid)
    assert (ep.status, ep.audio_path) == ("done", str(stuck))  # still done, still honest about the file


# ---- packaging -------------------------------------------------------------


def test_senko_is_pinned_to_a_commit():
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]["apple"]
    senko = next(d for d in deps if d.startswith("senko"))
    assert re.search(r"git\+https://\S+@[0-9a-f]{40}$", senko), senko


def test_backfill_records_older_entries_once_and_sync_still_takes_only_new(tmp_path, monkeypatch):
    entries = [
        {"source_guid": f"ep-{n}", "title": f"Ep {n}", "published": None, "audio_url": f"http://a/{n}"}
        for n in range(5, 0, -1)  # newest first, as feeds present them
    ]
    monkeypatch.setattr("ripperr.feeds.parse_feed", lambda url, *_: ("Show", list(entries)))
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    fid = rip.add_feed("http://feed").id

    assert rip.sync() == 1
    assert rip.backfill(fid, 3) == 2
    assert rip.backfill(fid, 3) == 0
    assert {e.source_guid for e in rip.episodes()} == {"ep-5", "ep-4", "ep-3"}

    entries.insert(0, {"source_guid": "ep-6", "title": "Ep 6", "published": None, "audio_url": "http://a/6"})
    assert rip.sync() == 1
    with pytest.raises(ValueError):
        rip.backfill(fid, 0)
