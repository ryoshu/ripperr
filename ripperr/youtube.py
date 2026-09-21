"""YouTube ingestion via yt-dlp: playlist listing and audio download.

Needs the `youtube` extra (yt-dlp plus a JS runtime, deno, which yt-dlp requires
to resolve YouTube stream URLs). Audio is transcribed like any other episode.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
from pathlib import Path

import requests

_SKIP_TITLES = {None, "[Private video]", "[Deleted video]"}
_UA = "ripperr/0.1 (personal archival tool)"


class _MetaDescription(HTMLParser):
    def __init__(self):
        super().__init__()
        self.summary = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        values = dict(attrs)
        key = values.get("name") or values.get("property")
        if key in {"description", "og:description"} and values.get("content"):
            self.summary = html.unescape(values["content"])


def _page_summary(url: str) -> str | None:
    response = None
    try:
        response = requests.get(
            url, headers={"User-Agent": _UA}, timeout=(5, 5), stream=True
        )
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(8192):
            body.extend(chunk)
            lowered = body.lower()
            if (b'name="description"' in lowered or
                    b'property="og:description"' in lowered or
                    b"</head" in lowered or len(body) >= 1 * 1024 * 1024):
                break
        parser = _MetaDescription()
        parser.feed(bytes(body).decode(response.encoding or "utf-8", "ignore"))
        return parser.summary
    except requests.RequestException:
        return None
    finally:
        if response is not None:
            response.close()


def is_youtube(url: str) -> bool:
    return "youtube.com/" in url or "youtu.be/" in url


def _ydl(**opts):
    from yt_dlp import YoutubeDL  # lazy: optional extra

    return YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True,
                      "socket_timeout": 10, **opts})


def parse_playlist(url: str) -> tuple[str | None, list[dict]]:
    """Same shape as feeds.parse_feed. Entries come back newest first."""
    with _ydl(extract_flat=True) as y:
        info = y.extract_info(url, download=False)
    episodes = []
    for e in info.get("entries") or []:
        if e.get("title") in _SKIP_TITLES:
            continue
        video_url = f"https://www.youtube.com/watch?v={e['id']}"
        summary = e.get("description")
        if summary is None:
            summary = _page_summary(video_url)
        episodes.append(
            {
                "source_guid": f"yt:{e['id']}",
                "title": e["title"],
                "summary": summary,
                "published": None,
                "audio_url": e["url"],
                "source_url": video_url,
            }
        )
    return info.get("title"), episodes


def download(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with _ydl(format="bestaudio", outtmpl=str(dest_dir / "%(id)s.%(ext)s")) as y:
        info = y.extract_info(url, download=True)
        return Path(y.prepare_filename(info))
