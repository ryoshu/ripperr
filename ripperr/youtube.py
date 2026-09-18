"""YouTube ingestion via yt-dlp: playlist listing and audio download.

Needs the `youtube` extra (yt-dlp plus a JS runtime, deno, which yt-dlp requires
to resolve YouTube stream URLs). Audio is transcribed like any other episode.
"""

from __future__ import annotations

from pathlib import Path

_SKIP_TITLES = {None, "[Private video]", "[Deleted video]"}


def is_youtube(url: str) -> bool:
    return "youtube.com/" in url or "youtu.be/" in url


def _ydl(**opts):
    from yt_dlp import YoutubeDL  # lazy: optional extra

    return YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, **opts})


def parse_playlist(url: str) -> tuple[str | None, list[dict]]:
    """Same shape as feeds.parse_feed. Entries come back newest first; flat
    extraction has no upload dates, so `published` is None."""
    with _ydl(extract_flat=True) as y:
        info = y.extract_info(url, download=False)
    episodes = [
        {
            "source_guid": f"yt:{e['id']}",
            "title": e["title"],
            "published": None,
            "audio_url": e["url"],
        }
        for e in info.get("entries") or []
        if e.get("title") not in _SKIP_TITLES
    ]
    return info.get("title"), episodes


def download(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with _ydl(format="bestaudio", outtmpl=str(dest_dir / "%(id)s.%(ext)s")) as y:
        info = y.extract_info(url, download=True)
        return Path(y.prepare_filename(info))
