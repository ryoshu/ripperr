"""Small HTTP server for podcast feeds and transcript change consumers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import mimetypes
import shutil
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .api import ChangeLogPrunedError, LeaseError, ProcessingBusyError, Ripperr
from .config import Config
from .pipeline import WORKER_SCHEMAS


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
            if method == "GET" and self._dashboard_file(parsed.path):
                return
            write = write or parsed.path.startswith(_WORK_PREFIX)  # workers always need the token
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
            except (ProcessingBusyError, LeaseError) as exc:
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

        def _dashboard_file(self, path: str) -> bool:
            """Serve a file of the built dashboard, or index.html for its client
            routes. API paths and a server without a dashboard return False."""
            root = cfg.dashboard_dir
            if root is None or path == "/healthz" or path.startswith("/v1/"):
                return False
            root = root.resolve()
            target = (root / unquote(path).lstrip("/")).resolve()
            if not target.is_relative_to(root):
                return False  # falls through to the API's 404
            if not target.is_file():
                target = root / "index.html"
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        def _authorized(self, *, write: bool = False) -> bool:
            if write and not token:
                _json(self, {"error": "this endpoint requires the server to have a bearer token configured"},
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

            if _work_path(parsed.path, "audio"):
                lease_id = parse_qs(parsed.query).get("lease_id", [""])[0]
                wav = rip.work_audio(_work_path(parsed.path, "audio"), lease_id)
                with wav.open("rb") as fh:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(wav.stat().st_size))
                    self.end_headers()
                    shutil.copyfileobj(fh, self.wfile)
                return

            prefix = "/v1/episodes/"
            if parsed.path.startswith(prefix) and parsed.path.count("/") == 3:
                guid = unquote(parsed.path[len(prefix):])
                detail = rip.episode_detail(guid)
                if detail is None:
                    body = {"error": "not found"}
                    status = HTTPStatus.NOT_FOUND
                    etag = None
                else:
                    transcript = detail.transcript
                    body = _episode_json(
                        transcript.episode,
                        detail.feed,
                        include_audio=True,
                    )
                    body["corrections"] = [c.__dict__ for c in transcript.corrections]
                    body["turns"] = [t.__dict__ for t in transcript.turns]
                    body["speaker_names"] = [_speaker_json(name) for name in transcript.speaker_names]
                    body["speaker_matches"] = [
                        _speaker_match_json(match)
                        for match in detail.speaker_matches
                    ]
                    body["guest_hints"] = [hint.__dict__ for hint in detail.guest_hints]
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

            if parsed.path == _WORK_PREFIX + "claim":
                body = self._json_body()
                worker = body.get("worker")
                if not isinstance(worker, str) or not worker.strip():
                    raise ValueError("worker is required")
                if body.get("schema") not in WORKER_SCHEMAS:
                    _json(self, {"error": "unsupported schema", "supported": list(WORKER_SCHEMAS)},
                          HTTPStatus.BAD_REQUEST)
                    return
                claimed = rip.claim_work(worker.strip()[:100])
                if claimed is None:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                    return
                episode, lease_id, expires = claimed
                _json(self, {
                    "guid": episode.guid,
                    "lease_id": lease_id,
                    "lease_expires": expires,
                    "duration_s": episode.duration,
                })
                return

            if _work_path(parsed.path, "fail"):
                body = self._json_body()
                lease_id, error = body.get("lease_id"), body.get("error")
                if not isinstance(lease_id, str) or not isinstance(error, str):
                    raise ValueError("lease_id and error are required")
                rip.fail_work(_work_path(parsed.path, "fail"), lease_id, error)
                _json(self, {"ok": True})
                return

            if parsed.path != "/v1/feeds":
                _json(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
                return

            url, title, backfill = self._feed_payload()
            feed = rip.add_feed(url, title)
            body: dict[str, object] = {"feed": _feed_json(feed)}
            if backfill:
                try:
                    body["backfilled"] = rip.backfill(feed.id, backfill)
                except Exception:  # noqa: BLE001 - the feed is stored; only the fetch failed
                    body["error"] = "feed stored, but it could not be fetched for backfill"
                    _json(self, body, HTTPStatus.BAD_GATEWAY)
                    return
            _json(self, body, HTTPStatus.CREATED)

        def _put(self, rip: Ripperr, parsed) -> None:
            if _work_path(parsed.path, "result"):
                body = self._json_body(max_bytes=_MAX_RESULT_BYTES)
                episode = rip.submit_work(_work_path(parsed.path, "result"), body)
                _json(self, {"guid": episode.guid, "revision": episode.revision})
                return

            if parsed.path.startswith("/v1/episodes/") and "/speakers/" in parsed.path:
                guid, speaker = _speaker_path(parsed.path)
                name = self._speaker_payload()
                mapping = rip.set_speaker_name(guid, speaker, name)
                _json(self, {"speaker_name": _speaker_json(mapping)})
                return

            feed_id = _feed_id(parsed.path)
            url, title, _ = self._feed_payload()
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

        def _feed_payload(self) -> tuple[str, str | None, int | None]:
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
            backfill = body.get("backfill")
            if backfill is not None and (
                isinstance(backfill, bool) or not isinstance(backfill, int) or not 1 <= backfill <= 100
            ):
                raise ValueError("backfill must be an integer from 1 to 100")
            return url, title, backfill

        def _json_body(self, max_bytes: int = 16_384) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            if length <= 0 or length > max_bytes:
                raise ValueError(f"request body must be between 1 and {max_bytes} bytes")
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("request body must be JSON") from exc
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            return body

    return Handler


_WORK_PREFIX = "/v1/work/"
_MAX_RESULT_BYTES = 64 * 1024 * 1024


def _work_path(path: str, action: str) -> str | None:
    """The guid in /v1/work/{guid}/{action}, or None for any other path."""
    if not path.startswith(_WORK_PREFIX) or not path.endswith("/" + action):
        return None
    guid = unquote(path[len(_WORK_PREFIX):-len(action) - 1])
    return guid if guid and "/" not in guid else None


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


def _speaker_match_json(match) -> dict[str, object]:
    return {
        "episode_guid": match.episode_guid,
        "speaker": match.speaker,
        "name": match.name,
        "score": match.score,
        "sample_count": match.sample_count,
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
