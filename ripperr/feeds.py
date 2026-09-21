"""RSS ingestion and audio download.

Deliberately dumb: parse the feed, find the enclosure, stream it to disk. No
platform-specific resolvers — point this at the show's actual RSS feed.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

from . import youtube

UA = "ripperr/0.1 (personal archival tool)"
CHUNK = 1 << 16
MAX_DOWNLOAD_BYTES = 1 << 30


def _slug(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "").strip()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:limit].strip("-").lower() or "episode"


def _published(entry) -> str | None:
    """ISO 8601 UTC, so the stored string sorts chronologically. Feeds give RFC 2822
    text ("Thu, 18 Sep 2025 ..."), which sorts by weekday name."""
    t = getattr(entry, "published_parsed", None)
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat() if t else None


def _enclosure_url(entry) -> str | None:
    for enc in getattr(entry, "enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "").lower()
        if href and (typ.startswith("audio") or not typ):
            return href
    for link in getattr(entry, "links", []) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return None


def _source_url(entry) -> str | None:
    """Return the public episode page, never an enclosure URL."""
    enclosure = _enclosure_url(entry)
    link = getattr(entry, "link", None)
    if link and str(link) != enclosure:
        return str(link)
    for candidate in getattr(entry, "links", []) or []:
        if candidate.get("rel") in (None, "alternate") and candidate.get("href"):
            href = str(candidate["href"])
            if href != enclosure:
                return href
    return None


def _summary(entry) -> str | None:
    return getattr(entry, "summary", None) or getattr(entry, "description", None)


def parse_feed(url: str) -> tuple[str | None, list[dict]]:
    """Return (feed_title, episodes). Episodes are dicts, newest first as the
    feed presents them."""
    if youtube.is_youtube(url):
        return youtube.parse_playlist(url)
    parsed = feedparser.parse(url, agent=UA)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"could not parse feed {url}: {parsed.bozo_exception}")

    title = getattr(parsed.feed, "title", None)
    episodes = []
    for entry in parsed.entries:
        audio_url = _enclosure_url(entry)
        if not audio_url:
            continue
        # Some feeds omit guid, or reuse it carelessly. Fall back to a hash of
        # the audio URL so we still deduplicate reliably.
        guid = getattr(entry, "id", None) or getattr(entry, "guid", None)
        if not guid:
            guid = hashlib.sha1(audio_url.encode()).hexdigest()
        episodes.append(
            {
                "source_guid": str(guid),
                "title": getattr(entry, "title", None),
                "summary": _summary(entry),
                "published": _published(entry),
                "audio_url": audio_url,
                "source_url": _source_url(entry),
            }
        )
    return title, episodes


def download(audio_url: str, dest_dir: Path, title: str | None) -> Path:
    """Stream an episode to disk. Returns the path to the raw downloaded file."""
    if youtube.is_youtube(audio_url):
        return youtube.download(audio_url, dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(audio_url.encode()).hexdigest()[:10]
    suffix = Path(audio_url.split("?")[0]).suffix or ".mp3"
    dest = dest_dir / f"{_slug(title or '')}-{digest}{suffix}"

    if dest.exists() and dest.stat().st_size > 0:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(
            audio_url, stream=True, timeout=60, headers={"User-Agent": UA}
        ) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            try:
                content_length = int(content_length) if content_length else None
            except ValueError:
                content_length = None
            if content_length is not None and content_length > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("download exceeds the 1 GiB limit")
            total = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(CHUNK):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("download exceeds the 1 GiB limit")
                    fh.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.rename(dest)
    return dest
