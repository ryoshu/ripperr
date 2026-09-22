"""Small HTTP server for podcast feeds and transcript change consumers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .api import ChangeLogPrunedError, ProcessingBusyError, Ripperr
from .config import Config


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


def make_handler(cfg: Config, token: str | None = None):
    def open_ripperr() -> Ripperr:
        return Ripperr(cfg, log=lambda _: None)

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
            parsed = urlparse(self.path)
            speaker_write = write and _is_speaker_path(parsed.path)
            if not self._authorized(write=write and not speaker_write):
                return
            try:
                rip = open_ripperr()
                try:
                    getattr(self, f"_{method.lower()}")(rip, parsed)
                finally:
                    rip.close()
            except LookupError as exc:
                _json(self, {"error": str(exc)}, HTTPStatus.NOT_FOUND)
            # Keep typed operational errors ahead of the generic HTTP fallbacks.
            except ProcessingBusyError as exc:
                _json(self, {"error": str(exc)}, HTTPStatus.CONFLICT)
            except ChangeLogPrunedError as exc:
                _json(
                    self,
                    {
                        "error": str(exc),
                        "reset": True,
                        "pruned_through": exc.pruned_through,
                    },
                    HTTPStatus.GONE,
                )
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
                        transcript.episode,
                        rip.feed(transcript.episode.feed_id),
                        include_audio=True,
                    )
                    body["corrections"] = [c.__dict__ for c in transcript.corrections]
                    body["turns"] = [t.__dict__ for t in transcript.turns]
                    body["speaker_names"] = [_speaker_json(name) for name in transcript.speaker_names]
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
            if parsed.path == "/v1/changes/bootstrap":
                after = rip.change_seq()
                emitted = rip.emit_current()
                _json(self, {
                    "emitted": emitted,
                    "after": after,
                    "next_cursor": rip.change_seq(),
                })
                return

            if parsed.path != "/v1/feeds":
                _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
                return

            url, title = self._feed_payload()
            feed = rip.add_feed(url, title)
            _json(self, {"feed": _feed_json(feed)}, HTTPStatus.CREATED)

        def _put(self, rip: Ripperr, parsed) -> None:
            if parsed.path.startswith("/v1/episodes/") and "/speakers/" in parsed.path:
                guid, speaker = _speaker_path(parsed.path)
                name = self._speaker_payload()
                mapping = rip.set_speaker_name(guid, speaker, name)
                _json(self, {"speaker_name": _speaker_json(mapping)})
                return

            feed_id = _feed_id(parsed.path)
            url, title = self._feed_payload()
            feed = rip.update_feed(feed_id, url, title)
            _json(self, {"feed": _feed_json(feed)})

        def _delete(self, rip: Ripperr, parsed) -> None:
            if parsed.path.startswith("/v1/episodes/") and "/speakers/" in parsed.path:
                guid, speaker = _speaker_path(parsed.path)
                rip.delete_speaker_name(guid, speaker)
                _json(self, {"deleted": speaker})
                return

            feed_id = _feed_id(parsed.path)
            rip.delete_feed(feed_id)
            _json(self, {"deleted": feed_id})

        def _speaker_payload(self) -> str:
            body = self._json_body()
            if not isinstance(body.get("name"), str):
                raise ValueError("name is required")
            name = body["name"].strip()
            if not name:
                raise ValueError("name is required")
            return name

        def _feed_payload(self) -> tuple[str, str | None]:
            body = self._json_body()
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

        def _json_body(self) -> dict[str, object]:
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
            return body

    return Handler


def _feed_json(feed) -> dict[str, object]:
    return {"id": feed.id, "url": feed.url, "title": feed.title}


def _episode_json(episode, feed, *, include_audio: bool = False) -> dict[str, object]:
    body = {
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
    if include_audio:
        body["audio_url"] = episode.audio_url
    return body


def _speaker_json(mapping) -> dict[str, object]:
    return {
        "episode_guid": mapping.episode_guid,
        "speaker": mapping.speaker,
        "name": mapping.name,
        "method": mapping.method,
        "confidence": mapping.confidence,
        "updated_at": mapping.updated_at,
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


def _speaker_path(path: str) -> tuple[str, str]:
    prefix = "/v1/episodes/"
    marker = "/speakers/"
    if not path.startswith(prefix) or marker not in path:
        raise ValueError("speaker path must include an episode and speaker")
    guid, speaker = path[len(prefix):].split(marker, 1)
    if not guid or not speaker or "/" in speaker:
        raise ValueError("speaker path must include an episode and speaker")
    return unquote(guid), unquote(speaker)


def _is_speaker_path(path: str) -> bool:
    return path.startswith("/v1/episodes/") and "/speakers/" in path


def _integer(query, name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = query.get(name, [str(default)])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} out of range")
    return value


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765,
          token: str | None = None, emit_current: bool = False) -> None:
    if not _loopback(host):
        raise ValueError(
            "--host must be loopback; use a TLS reverse proxy or tunnel for remote consumers"
        )
    if emit_current:
        with Ripperr(cfg, log=lambda _: None) as rip:
            rip.emit_current()
    server = ThreadingHTTPServer((host, port), make_handler(cfg, token))
    print(f"ripperr serving on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
