import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ripperr.models import Turn
from ripperr.server import make_handler, serve
from ripperr.store import Store


def _server(db, token=None):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, token))
    except PermissionError:
        pytest.skip("the test sandbox does not permit binding a local socket")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(server, path, headers=None):
    req = Request(f"http://127.0.0.1:{server.server_port}{path}", headers=headers or {})
    try:
        with urlopen(req) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else None


def test_transcript_and_metadata_events_are_transactional(tmp_path):
    store = Store(tmp_path / "r.db")
    feed = store.add_feed("https://feed")
    assert store.add_episode(feed.id, "one", "One", None, "https://audio", "https://show/one")
    episode = store.episode_by_id(1)
    assert [(x["kind"], x["revision"]) for x in store.changes()] == [("metadata", 0)]

    with pytest.raises(Exception):
        store.replace_turns(episode.id, [Turn(0, "S", 0, 1, None)])
    assert store.episode_by_id(1).revision == 0
    assert len(store.changes()) == 1

    store.replace_turns(episode.id, [Turn(0, "S", 0, 1, "hello")])
    assert [(x["kind"], x["revision"]) for x in store.changes()] == [
        ("metadata", 0), ("transcript", 1)]
    store.add_feed("https://feed", "Renamed Show")
    assert store.changes()[-1]["kind"] == "metadata"


def test_server_change_feed_and_revision_etag(tmp_path):
    db = tmp_path / "r.db"
    store = Store(db)
    feed = store.add_feed("https://feed", "Show")
    store.add_episode(feed.id, "one", "One", None, "https://audio", "https://show/one")
    ep = store.episode_by_id(1)
    store.replace_turns(ep.id, [Turn(0, "SPEAKER_00", 0, 1, "hello")])
    store.close()

    server, thread = _server(db)
    try:
        status, _, health = _get(server, "/healthz")
        assert status == 200 and health["change_seq"] == 2

        status, _, page = _get(server, "/v1/changes?after=0&limit=1")
        assert status == 200 and page["next_cursor"] == 1 and page["has_more"]
        assert page["changes"][0]["kind"] == "metadata"

        status, headers, body = _get(server, f"/v1/episodes/{ep.guid}")
        assert status == 200 and body["source_url"] == "https://show/one"
        assert "audio_path" not in body and "audio_url" not in body
        status, unchanged_headers, body = _get(
            server, f"/v1/episodes/{ep.guid}", {"If-None-Match": headers["ETag"]})
        assert status == 304 and body is None
        assert unchanged_headers["ETag"] == headers["ETag"]
    finally:
        server.shutdown()
        thread.join()


def test_non_loopback_handler_requires_token(tmp_path):
    db = tmp_path / "r.db"
    Store(db).close()
    server, thread = _server(db, "secret")
    try:
        status, _, _ = _get(server, "/healthz")
        assert status == 401
        status, _, body = _get(server, "/healthz", {"Authorization": "Bearer secret"})
        assert status == 200 and body["ok"]
    finally:
        server.shutdown()
        thread.join()


def test_episode_etag_tracks_episode_feed_and_transcript_metadata(tmp_path):
    db = tmp_path / "r.db"
    store = Store(db)
    feed = store.add_feed("https://feed", "Show")
    store.add_episode(feed.id, "one", "One", None, "https://audio", "https://show/one")
    episode = store.episode_by_id(1)
    store.close()

    server, thread = _server(db)
    try:
        status, headers, body = _get(server, f"/v1/episodes/{episode.guid}")
        assert status == 200 and body["title"] == "One"
        old_etag = headers["ETag"]

        store = Store(db)
        store.add_episode(feed.id, "one", "Two", None, "https://audio", "https://show/two")
        store.close()

        status, headers, body = _get(
            server, f"/v1/episodes/{episode.guid}", {"If-None-Match": old_etag})
        assert status == 200 and headers["ETag"] != old_etag
        assert body["title"] == "Two" and body["source_url"] == "https://show/two"
        episode_etag = headers["ETag"]

        store = Store(db)
        store.add_feed("https://feed", "Renamed Show")
        store.close()

        status, headers, body = _get(
            server, f"/v1/episodes/{episode.guid}", {"If-None-Match": episode_etag})
        assert status == 200 and headers["ETag"] != episode_etag
        assert body["feed"]["title"] == "Renamed Show"
        feed_etag = headers["ETag"]

        store = Store(db)
        store.replace_turns(store.episode_by_id(1).id, [Turn(0, "S", 0, 1, "hello")])
        store.close()

        status, headers, body = _get(
            server, f"/v1/episodes/{episode.guid}", {"If-None-Match": feed_etag})
        assert status == 200 and headers["ETag"] != feed_etag
        assert body["turns"][0]["text"] == "hello"
    finally:
        server.shutdown()
        thread.join()


def test_serve_rejects_plaintext_remote_bind(tmp_path):
    with pytest.raises(ValueError, match="TLS reverse proxy or tunnel"):
        serve(tmp_path / "r.db", host="0.0.0.0", token="secret")
