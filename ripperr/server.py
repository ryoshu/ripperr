"""Small HTTP server for podcast feeds and transcript change consumers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .api import ProcessingBusyError, Ripperr
from .config import Config, default_config
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


def make_handler(db_path: Path, token: str | None = None, cfg: Config | None = None):
    cfg = cfg or default_config()
    cfg.root = db_path.parent

    def open_ripperr() -> Ripperr:
        return Ripperr(cfg, store=Store(db_path), log=lambda _: None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ripperr/0.1"

        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST", write=True)

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT", write=True)

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE", write=True)

        def _dispatch(self, method: str, *, write: bool = False) -> None:
            if not self._authorized(write=write):
                return
            parsed = urlparse(self.path)
            try:
                rip = open_ripperr()
                try:
                    getattr(self, f"_{method.lower()}")(rip, parsed)
                finally:
                    rip.close()
            except LookupError as exc:
                _json(self, {"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ProcessingBusyError as exc:
                _json(self, {"error": str(exc)}, HTTPStatus.CONFLICT)
            except ValueError as exc:
                _json(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception:  # noqa: BLE001 - never leak a traceback or host path over HTTP
                _json(self, {"error": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _authorized(self, *, write: bool = False) -> bool:
            if write and not token:
                _json(self, {"error": "feed management requires a bearer token"},
                      HTTPStatus.SERVICE_UNAVAILABLE)
                return False
            if token and not hmac.compare_digest(
                self.headers.get("Authorization", ""), f"Bearer {token}"
            ):
                _json(self, {"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED,
                      {"WWW-Authenticate": "Bearer"})
                return False
            return True

        def _get(self, rip: Ripperr, parsed) -> None:
            if parsed.path == "/healthz":
                _json(self, {"ok": True, "change_seq": rip.change_seq()})
                return

            if parsed.path == "/v1/changes":
                query = parse_qs(parsed.query)
                after = _integer(query, "after", 0, 0)
                limit = _integer(query, "limit", 100, 1, 1000)
                changes = rip.changes(after, limit)
                next_cursor = changes[-1].seq if changes else after
                _json(self, {
                    "changes": [_change_json(change) for change in changes],
                    "next_cursor": next_cursor,
                    "has_more": bool(changes) and next_cursor < rip.change_seq(),
                })
                return

            if parsed.path == "/v1/feeds":
                _json(self, {"feeds": [_feed_json(feed) for feed in rip.feeds()]})
                return

            if parsed.path == "/v1/episodes":
                query = parse_qs(parsed.query)
                after = _integer(query, "after", 0, 0)
                limit = _integer(query, "limit", 100, 1, 1000)
                episodes = rip.episodes(after=after, limit=limit + 1)
                has_more = len(episodes) > limit
                episodes = episodes[:limit]
                next_cursor = episodes[-1].id if episodes else after
                feeds = {feed.id: feed for feed in rip.feeds()}
                _json(self, {
                    "episodes": [_episode_json(episode, feeds[episode.feed_id]) for episode in episodes],
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                })
                return

            prefix = "/v1/episodes/"
            if parsed.path.startswith(prefix) and parsed.path.count("/") == 3:
                guid = unquote(parsed.path[len(prefix):])
                transcript = rip.transcript(guid)
                if transcript is None:
                    body = {"error": "not found"}
                    status = HTTPStatus.NOT_FOUND
                    etag = None
                else:
                    body = _episode_json(
                        transcript.episode, rip.feed(transcript.episode.feed_id)
                    )
                    body["corrections"] = [c.__dict__ for c in transcript.corrections]
                    body["turns"] = [t.__dict__ for t in transcript.turns]
                    status = HTTPStatus.OK
                    etag = '"' + hashlib.sha256(json.dumps(
                        body, sort_keys=True, separators=(",", ":"), default=str
                    ).encode()).hexdigest()[:32] + '"'
                if status == HTTPStatus.NOT_FOUND:
                    _json(self, body, status)
                    return
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                _json(self, body, headers={"ETag": etag})
                return

            _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

        def _post(self, rip: Ripperr, parsed) -> None:
            if parsed.path != "/v1/feeds":
                _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
                return

            url, title = self._feed_payload()
            feed = rip.add_feed(url, title)
            _json(self, {"feed": _feed_json(feed)}, HTTPStatus.CREATED)

        def _put(self, rip: Ripperr, parsed) -> None:
            feed_id = _feed_id(parsed.path)
            url, title = self._feed_payload()
            feed = rip.update_feed(feed_id, url, title)
            _json(self, {"feed": _feed_json(feed)})

        def _delete(self, rip: Ripperr, parsed) -> None:
            feed_id = _feed_id(parsed.path)
            rip.delete_feed(feed_id)
            _json(self, {"deleted": feed_id})

        def _feed_payload(self) -> tuple[str, str | None]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            if length <= 0 or length > 16_384:
                raise ValueError("request body must be between 1 and 16384 bytes")
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("request body must be JSON") from exc
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")

            url = body.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("url is required")
            url = url.strip()
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError("url must be an http or https URL")
            # Defense-in-depth typo guard only: DNS answers and redirects are
            # intentionally not inspected here.
            hostname = parsed_url.hostname
            hostname = hostname.lower().rstrip(".") if hostname else None
            if hostname is None or hostname == "localhost" or hostname.endswith(".local"):
                raise ValueError("url host must not be a local/private literal address")
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                address = None
            if address is not None and (
                address.is_loopback
                or address.is_private
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ValueError("url host must not be a local/private literal address")

            title = body.get("title")
            if title is not None and not isinstance(title, str):
                raise ValueError("title must be a string")
            title = title.strip() if title else None
            return url, title

    return Handler


def _feed_json(feed) -> dict[str, object]:
    return {"id": feed.id, "url": feed.url, "title": feed.title}


def _episode_json(episode, feed) -> dict[str, object]:
    return {
        "guid": episode.guid,
        "source_guid": episode.source_guid,
        "feed": _feed_json(feed),
        "title": episode.title,
        "summary": episode.summary,
        "published": episode.published,
        "source_url": episode.source_url,
        "duration": episode.duration,
        "status": episode.status,
        "updated_at": episode.updated_at,
        "revision": episode.revision,
        "merged_at": episode.merged_at,
    }


def _change_json(change) -> dict[str, object]:
    return {
        "seq": change.seq,
        "episode_guid": change.episode_guid,
        "revision": change.revision,
        "kind": change.kind,
        "occurred_at": change.occurred_at,
    }


def _feed_id(path: str) -> int:
    prefix = "/v1/feeds/"
    if not path.startswith(prefix) or path.count("/") != 3:
        raise ValueError("feed path must include an id")
    try:
        feed_id = int(path[len(prefix):])
    except ValueError as exc:
        raise ValueError("feed id must be an integer") from exc
    if feed_id < 1:
        raise ValueError("feed id must be positive")
    return feed_id


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
          token: str | None = None, emit_current: bool = False,
          cfg: Config | None = None) -> None:
    if not _loopback(host):
        raise ValueError(
            "--host must be loopback; use a TLS reverse proxy or tunnel for remote consumers"
        )
    cfg = cfg or default_config()
    cfg.root = db_path.parent
    if emit_current:
        with Ripperr(cfg, log=lambda _: None) as rip:
            rip.emit_current()
    server = ThreadingHTTPServer((host, port), make_handler(db_path, token, cfg))
    print(f"ripperr serving on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
