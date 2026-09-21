"""YouTube ingestion via yt-dlp: playlist listing and audio download.

Needs the `youtube` extra (yt-dlp plus a JS runtime, deno, which yt-dlp requires
to resolve YouTube stream URLs). Audio is transcribed like any other episode.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

_SKIP_TITLES = {None, "[Private video]", "[Deleted video]"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def is_youtube(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") in _YOUTUBE_HOSTS
    )


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
        episodes.append(
            {
                "source_guid": f"yt:{e['id']}",
                "title": e["title"],
                "summary": e.get("description"),
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
