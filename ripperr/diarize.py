"""Speaker diarization adapters with normalized speaker segments.

Senko is a tuned fork of the 3D-Speaker pipeline: pyannote segmentation-3.0 for
VAD, CAM++ for embeddings, spectral or UMAP+HDBSCAN clustering. On macOS both
models run through CoreML rather than PyTorch, which is where the speedup comes
from — roughly an hour of audio in single-digit seconds on an M3.

The diarizer holds model weights, so build it once and reuse it across episodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import resolve_diarization_backend

_START_KEYS = ("start", "start_time", "begin")
_END_KEYS = ("end", "stop", "end_time")
_SPEAKER_KEYS = ("speaker", "speaker_id", "label")


class Diarizer:
    def __init__(
        self,
        device: str = "auto",
        warmup: bool = True,
        quiet: bool = True,
        backend: str = "auto",
        model: str = "pyannote/speaker-diarization-community-1",
        token: str | None = None,
    ):
        self.backend = backend_name(backend)
        if self.backend == "senko":
            import senko  # lazy import

            self._impl = senko.Diarizer(device=device, warmup=warmup, quiet=quiet)
        else:
            from pyannote.audio import Pipeline  # lazy import for the base install
            import torch

            self._impl = Pipeline.from_pretrained(model, token=token)
            if self._impl is None:
                raise RuntimeError(f"could not load diarization model {model}")
            target = "cuda" if device == "cuda" or (device == "auto" and torch.cuda.is_available()) else "cpu"
            self._impl.to(torch.device(target))

    def run(self, wav_path: Path) -> list[dict[str, Any]]:
        if self.backend == "senko":
            result = self._impl.diarize(str(wav_path), generate_colors=False)
            raw = result.get("merged_segments") or result.get("segments") or []
        else:
            result = self._impl(str(wav_path))
            annotation = _pyannote_annotation(result)
            raw = [
                {"start": turn.start, "end": turn.end, "speaker": speaker}
                for turn, _, speaker in annotation.itertracks(yield_label=True)
            ] if annotation is not None else []
        return normalize_segments(raw)

    def run_with_embeddings(
        self, wav_path: Path
    ) -> tuple[list[dict[str, Any]], dict[str, list[float]]]:
        """Return normalized segments and Senko's per-speaker CAM++ centroids."""
        if self.backend != "senko":
            raise RuntimeError("speaker embeddings require the senko backend")
        result = self._impl.diarize(str(wav_path), generate_colors=False)
        raw = result.get("merged_segments") or result.get("segments") or []
        return normalize_segments(raw), normalize_embeddings(result.get("speaker_centroids"))


def backend_name(backend: str = "auto") -> str:
    return resolve_diarization_backend(backend)


def _pyannote_annotation(result: Any) -> Any:
    if isinstance(result, dict):
        for key in ("exclusive_speaker_diarization", "speaker_diarization"):
            if result.get(key) is not None:
                return result[key]
        return None
    for key in ("exclusive_speaker_diarization", "speaker_diarization"):
        annotation = getattr(result, key, None)
        if annotation is not None:
            return annotation
    return None


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


def normalize_embeddings(centroids: Any) -> dict[str, list[float]]:
    """Convert Senko/Numpy centroid values into JSON-safe speaker vectors."""
    if not isinstance(centroids, dict):
        return {}
    result = {}
    for speaker, embedding in centroids.items():
        values = embedding.tolist() if hasattr(embedding, "tolist") else embedding
        result[_label(speaker)] = [float(value) for value in values]
    return result
