"""Small read-only HTTP change-feed server for podcast consumers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .store import Store


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _json(handler: BaseHTTPRequestHandler, value: object, status: int = 200, headers=None) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    for key, val in (headers or {}).items():
        handler.send_header(key, val)
    handler.end_headers()
    handler.wfile.write(payload)


def make_handler(db_path: Path, token: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ripperr/0.1"

        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                _json(self, {"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED,
                      {"WWW-Authenticate": "Bearer"})
                return
            parsed = urlparse(self.path)
            try:
                store = Store(db_path)
                try:
                    self._get(store, parsed)
                finally:
                    store.close()
            except ValueError as exc:
                _json(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception:  # noqa: BLE001 - never leak a traceback or host path over HTTP
                _json(self, {"error": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _get(self, store: Store, parsed) -> None:
            if parsed.path == "/healthz":
                _json(self, {"ok": True, "change_seq": store.highest_change_seq()})
                return

            if parsed.path == "/v1/changes":
                query = parse_qs(parsed.query)
                after = _integer(query, "after", 0, 0)
                limit = _integer(query, "limit", 100, 1, 1000)
                changes = store.changes(after, limit)
                next_cursor = changes[-1]["seq"] if changes else after
                _json(self, {
                    "changes": changes,
                    "next_cursor": next_cursor,
                    "has_more": bool(changes) and next_cursor < store.highest_change_seq(),
                })
                return

            prefix = "/v1/episodes/"
            if parsed.path.startswith(prefix) and parsed.path.count("/") == 3:
                guid = unquote(parsed.path[len(prefix):])
                episode = store.episode(guid)
                if episode is None:
                    _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
                feed = store.feed(episode.feed_id)
                body = {
                    "guid": episode.guid,
                    "source_guid": episode.source_guid,
                    "feed": {"id": feed.id, "url": feed.url, "title": feed.title},
                    "title": episode.title,
                    "published": episode.published,
                    "source_url": episode.source_url,
                    "duration": episode.duration,
                    "status": episode.status,
                    "updated_at": episode.updated_at,
                    "revision": episode.revision,
                    "merged_at": episode.merged_at,
                    "corrections": [c.__dict__ for c in store.corrections(episode.id)],
                    "turns": [t.__dict__ for t in store.turns(episode.id)],
                }
                etag = '"' + hashlib.sha256(json.dumps(
                    body, sort_keys=True, separators=(",", ":"), default=str
                ).encode()).hexdigest()[:32] + '"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                _json(self, body, headers={"ETag": etag})
                return

            _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def _integer(query, name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} out of range")
    return value


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765,
          token: str | None = None, emit_current: bool = False) -> None:
    if not _loopback(host):
        raise ValueError(
            "--host must be loopback; use a TLS reverse proxy or tunnel for remote consumers"
        )
    if emit_current:
        store = Store(db_path)
        try:
            store.emit_current()
        finally:
            store.close()
    server = ThreadingHTTPServer((host, port), make_handler(db_path, token))
    print(f"ripperr serving on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
