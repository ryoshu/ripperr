import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ripperr.models import Turn
from ripperr.config import Config
from ripperr.server import make_handler, serve
from ripperr.store import Store


def _server(db, token=None):
    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(Config(root=db.parent), token)
        )
    except PermissionError:
        pytest.skip("the test sandbox does not permit binding a local socket")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_make_handler_does_not_mutate_config(tmp_path):
    cfg = Config(root=tmp_path / "configured")
    make_handler(cfg)
    assert cfg.root == tmp_path / "configured"


def _get(server, path, headers=None):
    req = Request(f"http://127.0.0.1:{server.server_port}{path}", headers=headers or {})
    try:
        with urlopen(req) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else None


def _post(server, path, body, headers=None):
    req = Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(req) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else None


def _request(server, method, path, body=None, headers=None):
    req = Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urlopen(req) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else None


def test_transcript_and_metadata_events_are_transactional(tmp_path):
    store = Store(tmp_path / "ripperr.db")
    feed = store.add_feed("https://feed")
    assert store.add_episode(feed.id, "one", "One", None, "https://audio", "https://show/one")
    episode = store.episode_by_id(1)
    assert [(x.kind, x.revision) for x in store.changes()] == [("metadata", 0)]

    with pytest.raises(Exception):
        store.replace_turns(episode.id, [Turn(0, "S", 0, 1, None)])
    assert store.episode_by_id(1).revision == 0
    assert len(store.changes()) == 1

    store.replace_turns(episode.id, [Turn(0, "S", 0, 1, "hello")])
    assert [(x.kind, x.revision) for x in store.changes()] == [
        ("metadata", 0), ("transcript", 1)]
    store.add_feed("https://feed", "Renamed Show")
    assert store.changes()[-1].kind == "metadata"


def test_server_change_feed_and_revision_etag(tmp_path):
    db = tmp_path / "ripperr.db"
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


def test_pruned_change_cursor_requires_rebootstrap(tmp_path):
    db = tmp_path / "ripperr.db"
    store = Store(db)
    feed = store.add_feed("https://feed")
    store.add_episode(feed.id, "one", "One", None, "https://audio")
    assert store.prune_changes(1) == 1
    store.close()

    server, thread = _server(db)
    try:
        status, _, body = _get(server, "/v1/changes?after=0")
        assert status == 410
        assert body["reset"] is True and body["pruned_through"] == 1
    finally:
        server.shutdown()
        thread.join()


def test_episode_list_returns_feed_metadata(tmp_path):
    db = tmp_path / "ripperr.db"
    store = Store(db)
    feed = store.add_feed("https://feed", "Show")
    store.add_episode(feed.id, "one", "One", None, "https://audio", "https://show/one")
    guid = store.episode_by_id(1).guid
    store.close()

    server, thread = _server(db, "secret")
    try:
        status, _, body = _get(server, "/v1/episodes", {"Authorization": "Bearer secret"})
        assert status == 200
        assert body["next_cursor"] == 1 and not body["has_more"]
        assert body["episodes"] == [{
            "guid": guid,
            "source_guid": "one",
            "feed": {"id": 1, "url": "https://feed", "title": "Show"},
            "title": "One",
            "summary": None,
            "published": None,
            "source_url": "https://show/one",
            "duration": None,
            "status": "new",
            "updated_at": body["episodes"][0]["updated_at"],
            "revision": 0,
            "merged_at": None,
        }]
        assert "turns" not in body["episodes"][0]
    finally:
        server.shutdown()
        thread.join()


def test_non_loopback_handler_requires_token(tmp_path):
    db = tmp_path / "ripperr.db"
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


def test_feed_management_lists_and_adds_with_token(tmp_path):
    db = tmp_path / "ripperr.db"
    Store(db).close()
    server, thread = _server(db, "secret")
    try:
        status, _, body = _get(server, "/v1/feeds", {"Authorization": "Bearer secret"})
        assert status == 200 and body == {"feeds": []}

        status, _, body = _post(
            server,
            "/v1/feeds",
            {"url": "https://example.com/feed.xml", "title": "Example"},
            {"Authorization": "Bearer secret"},
        )
        assert status == 201
        assert body["feed"] == {"id": 1, "url": "https://example.com/feed.xml", "title": "Example"}

        status, _, body = _get(server, "/v1/feeds", {"Authorization": "Bearer secret"})
        assert status == 200 and body["feeds"] == [body["feeds"][0]]
        assert body["feeds"][0]["url"] == "https://example.com/feed.xml"
    finally:
        server.shutdown()
        thread.join()


def test_feed_management_updates_and_deletes_with_token(tmp_path):
    db = tmp_path / "ripperr.db"
    store = Store(db)
    store.add_feed("https://example.com/feed.xml", "Example")
    store.close()
    server, thread = _server(db, "secret")
    try:
        status, _, body = _request(
            server,
            "PUT",
            "/v1/feeds/1",
            {"url": "https://example.com/renamed.xml", "title": "Renamed"},
            {"Authorization": "Bearer secret"},
        )
        assert status == 200
        assert body["feed"] == {
            "id": 1, "url": "https://example.com/renamed.xml", "title": "Renamed"
        }

        status, _, body = _request(
            server, "DELETE", "/v1/feeds/1", headers={"Authorization": "Bearer secret"}
        )
        assert status == 200 and body == {"deleted": 1}

        status, _, body = _get(server, "/v1/feeds", {"Authorization": "Bearer secret"})
        assert status == 200 and body == {"feeds": []}

        status, _, _ = _request(
            server, "DELETE", "/v1/feeds/1", headers={"Authorization": "Bearer secret"}
        )
        assert status == 404
    finally:
        server.shutdown()
        thread.join()


def test_feed_management_requires_token_even_on_loopback(tmp_path):
    db = tmp_path / "ripperr.db"
    Store(db).close()
    server, thread = _server(db)
    try:
        status, _, body = _post(server, "/v1/feeds", {"url": "https://example.com/feed.xml"})
        assert status == 503 and "bearer token" in body["error"]
    finally:
        server.shutdown()
        thread.join()


def test_feed_management_rejects_private_hosts(tmp_path):
    db = tmp_path / "ripperr.db"
    Store(db).close()
    server, thread = _server(db, "secret")
    try:
        status, _, body = _post(
            server,
            "/v1/feeds",
            {"url": "http://127.0.0.1:8000/private.xml"},
            {"Authorization": "Bearer secret"},
        )
        assert status == 400 and "local/private literal" in body["error"]
    finally:
        server.shutdown()
        thread.join()


def test_episode_etag_tracks_episode_feed_and_transcript_metadata(tmp_path):
    db = tmp_path / "ripperr.db"
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


def test_episode_response_uses_one_snapshot(tmp_path, monkeypatch):
    db = tmp_path / "ripperr.db"
    store = Store(db)
    feed = store.add_feed("https://feed", "Show")
    store.add_episode(feed.id, "one", "One", None, "https://audio", "https://show/one")
    episode = store.episode_by_id(1)
    store.replace_turns(episode.id, [Turn(0, "S", 0, 1, "old")])
    store.close()

    original_turns = Store.turns
    replaced = False

    def replace_before_read(current, episode_id):
        nonlocal replaced
        if not replaced:
            replaced = True
            writer = Store(db)
            writer.replace_turns(episode_id, [Turn(0, "S", 0, 1, "new")])
            writer.close()
        return original_turns(current, episode_id)

    monkeypatch.setattr(Store, "turns", replace_before_read)
    server, thread = _server(db)
    try:
        status, _, body = _get(server, f"/v1/episodes/{episode.guid}")
        assert status == 200
        assert (body["revision"], body["turns"][0]["text"]) in ((1, "old"), (2, "new"))

        status, _, body = _get(server, f"/v1/episodes/{episode.guid}")
        assert status == 200 and body["revision"] == 2
        assert body["turns"][0]["text"] == "new"
    finally:
        server.shutdown()
        thread.join()


def test_serve_rejects_plaintext_remote_bind(tmp_path):
    with pytest.raises(ValueError, match="TLS reverse proxy or tunnel"):
        serve(Config(root=tmp_path), host="0.0.0.0", token="secret")
