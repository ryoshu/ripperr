"""RSS ingestion and audio download.

Deliberately dumb: parse the feed, find the enclosure, stream it to disk. No
platform-specific resolvers — point this at the show's actual RSS feed.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import feedparser
import requests

from . import youtube

UA = "ripperr/0.1 (personal archival tool)"
CHUNK = 1 << 16


def _slug(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "").strip()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:limit].strip("-").lower() or "episode"


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
                "guid": str(guid),
                "title": getattr(entry, "title", None),
                "published": getattr(entry, "published", None),
                "audio_url": audio_url,
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
    with requests.get(
        audio_url, stream=True, timeout=60, headers={"User-Agent": UA}
    ) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(CHUNK):
                fh.write(chunk)
    tmp.rename(dest)
    return dest
