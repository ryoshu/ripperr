import json

from ripperr.api import Ripperr
from ripperr.config import Config

GUID = "ep-1"


def make(tmp_path):
    """A Ripperr with one episode whose model output is already cached, so
    remerge runs without any model, network or audio."""
    cfg = Config(root=tmp_path)
    rip = Ripperr(cfg, log=lambda _: None)
    fid = rip.add_feed("http://feed").id
    rip.store.add_episode(fid, GUID, "Ep 1", None, "http://a")
    words = [
        {"start": 0, "end": 0.4, "word": "Basial"}, {"start": 0.4, "end": 0.9, "word": "Tootin"},
        {"start": 1, "end": 1.5, "word": "runs"},
    ]
    cfg.raw_path(GUID, "asr").write_text(json.dumps({"segments": [{"words": words}]}))
    cfg.raw_path(GUID, "diar").write_text(json.dumps([{"start": 0, "end": 2, "speaker": "SPEAKER_01"}]))
    return rip


def test_remerge_applies_glossary_and_bumps_revision(tmp_path):
    rip = make(tmp_path)
    assert rip.episode(GUID).revision == 0

    ep = rip.remerge(GUID, glossary=["Bhayshul Tuten"])
    assert (ep.status, ep.revision) == ("done", 1)
    tr = rip.transcript(GUID)
    assert [t.text for t in tr.turns] == ["Bhayshul Tuten runs"]
    assert [(c.heard, c.fixed, c.count) for c in tr.corrections] == [("Basial Tootin", "Bhayshul Tuten", 1)]

    # a second remerge without a glossary rewrites the transcript: new revision, no corrections
    ep = rip.remerge(GUID, glossary=[])
    assert ep.revision == 2
    tr = rip.transcript(GUID)
    assert tr.turns[0].text == "Basial Tootin runs" and tr.corrections == []


def test_glossary_file_is_the_default(tmp_path):
    rip = make(tmp_path)
    rip.cfg.glossary_path.write_text("# players\nBhayshul Tuten\n\n")
    rip.remerge(GUID)
    assert rip.transcript(GUID).turns[0].text == "Bhayshul Tuten runs"


def test_guid_and_local_id_both_address_an_episode(tmp_path):
    rip = make(tmp_path)
    ep = rip.episode(GUID)
    assert rip.episode(ep.id).guid == GUID
    assert rip.episode("nope") is None and rip.transcript("nope") is None


def test_episodes_filters_by_status_and_update_time(tmp_path):
    rip = make(tmp_path)
    assert rip.episodes(status="done") == []
    rip.remerge(GUID, glossary=[])
    done = rip.episodes(status="done")
    assert [e.guid for e in done] == [GUID]
    assert rip.episodes(updated_since=done[0].updated_at) == done
    assert rip.episodes(updated_since="2999-01-01T00:00:00+00:00") == []


def test_search_returns_typed_hits_with_guid(tmp_path):
    rip = make(tmp_path)
    rip.remerge(GUID, glossary=["Bhayshul Tuten"])
    (hit,) = rip.search("Tuten")
    assert hit.guid == GUID and "[Tuten]" in hit.snippet


def test_old_database_is_migrated(tmp_path):
    import sqlite3

    from ripperr.store import Store

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
    ep = Store(db).episode("g")
    assert (ep.revision, ep.merged_at) == (0, None)
