"""Audio normalization.

Senko wants 16 kHz mono 16-bit WAV specifically, and Whisper resamples to 16 kHz
anyway, so we convert once up front and feed the same file to both models.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH (brew install ffmpeg)")


def to_wav16k(src: Path, dest_dir: Path) -> Path:
    """Convert any input to 16 kHz mono signed 16-bit WAV."""
    _require_ffmpeg()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (src.stem + ".16k.wav")
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    tmp = dest.with_suffix(".tmp.wav")
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-i", str(src),
            "-ac", "1",
            "-ar", "16000",
            "-acodec", "pcm_s16le",
            str(tmp),
        ],
        check=True,
    )
    tmp.rename(dest)
    return dest


def duration_seconds(path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, ValueError):
        return None
