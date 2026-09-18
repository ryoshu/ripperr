"""YouTube ingestion via yt-dlp: playlist listing, audio download, auto-captions.

Needs the `youtube` extra (yt-dlp plus a JS runtime, deno, which yt-dlp requires
to resolve YouTube stream URLs). Captions are an optional shortcut past Whisper:
YouTube auto-captions carry word-level timing, so they can feed the same merge
stage, but they have no speakers. Diarization still runs on the audio.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

MIN_WORDS_PER_SEC = 1.0  # conversational speech runs ~2-3; below 1 the captions are sparse
_NOISE = re.compile(r"^(>>+|\[[^\]]*\])$")  # ">>" speaker-change marks, "[Music]" tags
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
            "guid": f"yt:{e['id']}",
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


def fetch_captions(url: str, lang: str = "en-orig") -> dict[str, Any] | None:
    """Auto-captions as a Whisper-shaped result, or None if unavailable."""
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "skip_download": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "json3",
            "outtmpl": f"{tmp}/c",
        }
        try:
            with _ydl(**opts) as y:
                y.extract_info(url, download=True)
        except Exception:  # noqa: BLE001 - captions are best-effort
            return None
        files = list(Path(tmp).glob("*.json3"))
        return json3_to_result(json.loads(files[0].read_text())) if files else None


def json3_to_result(data: dict[str, Any]) -> dict[str, Any] | None:
    """Convert YouTube json3 captions to {"segments": [{"words": [...]}]}.

    Returns None when the captions lack word-level timing, since segment-level
    timing is too coarse for the speaker merge.
    """
    words: list[dict[str, Any]] = []
    timed = multi = 0
    for ev in data.get("events", []):
        segs = [s for s in ev.get("segs") or [] if (s.get("utf8") or "").strip() and not _NOISE.match(s["utf8"].strip())]
        if len(segs) > 1:
            multi += 1
            timed += any("tOffsetMs" in s for s in segs[1:])
        for s in segs:
            start = (ev["tStartMs"] + s.get("tOffsetMs", 0)) / 1000
            words.append({"word": s["utf8"].strip(), "start": start})

    if not words or (multi and timed / multi < 0.5):
        return None

    words.sort(key=lambda w: w["start"])
    for w, nxt in zip(words, words[1:]):
        # ponytail: caption words carry no end time; cap at 1s so a gap doesn't
        # smear a word across silence. Real ends would need forced alignment.
        w["end"] = min(nxt["start"], w["start"] + 1.0)
    words[-1]["end"] = words[-1]["start"] + 0.5
    return {"source": "youtube-captions", "segments": [{"words": words}]}


def captions_reliable(result: dict[str, Any] | None, duration: float | None) -> bool:
    if not result:
        return False
    words = result["segments"][0]["words"]
    return not duration or len(words) / duration >= MIN_WORDS_PER_SEC
