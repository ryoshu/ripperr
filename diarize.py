"""Speaker diarization via Senko (CoreML on Apple Silicon).

Senko is a tuned fork of the 3D-Speaker pipeline: pyannote segmentation-3.0 for
VAD, CAM++ for embeddings, spectral or UMAP+HDBSCAN clustering. On macOS both
models run through CoreML rather than PyTorch, which is where the speedup comes
from — roughly an hour of audio in single-digit seconds on an M3.

The diarizer holds model weights, so build it once and reuse it across episodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_START_KEYS = ("start", "start_time", "begin")
_END_KEYS = ("end", "stop", "end_time")
_SPEAKER_KEYS = ("speaker", "speaker_id", "label")


class Diarizer:
    def __init__(self, device: str = "auto", warmup: bool = True, quiet: bool = True):
        import senko  # lazy import

        self._impl = senko.Diarizer(device=device, warmup=warmup, quiet=quiet)

    def run(self, wav_path: Path) -> list[dict[str, Any]]:
        result = self._impl.diarize(str(wav_path), generate_colors=False)
        raw = result.get("merged_segments") or result.get("segments") or []
        return normalize_segments(raw)


def normalize_segments(raw: Any) -> list[dict[str, Any]]:
    """Coerce diarizer output into [{start, end, speaker}], sorted by start.

    Senko's exact field names have shifted between versions, so we probe a few
    aliases rather than hard-coding one shape. If this raises, print one raw
    segment and add the key you see to the tuples above.
    """
    segments: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            start, end, speaker = item[0], item[1], item[2]
        elif isinstance(item, dict):
            start = _first(item, _START_KEYS)
            end = _first(item, _END_KEYS)
            speaker = _first(item, _SPEAKER_KEYS)
        else:
            continue

        if start is None or end is None:
            continue
        segments.append(
            {
                "start": float(start),
                "end": float(end),
                "speaker": _label(speaker),
            }
        )

    segments.sort(key=lambda s: (s["start"], s["end"]))
    return segments


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _label(speaker: Any) -> str:
    if speaker is None:
        return "SPEAKER_?"
    if isinstance(speaker, int):
        return f"SPEAKER_{speaker:02d}"
    text = str(speaker)
    return text if text.upper().startswith("SPEAKER") else f"SPEAKER_{text}"


def speaker_count(segments: list[dict[str, Any]]) -> int:
    return len({s["speaker"] for s in segments})
