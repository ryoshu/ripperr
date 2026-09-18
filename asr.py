"""Transcription via mlx-whisper (Metal GPU on Apple Silicon).

We only care about one thing here beyond the text: word-level timestamps. Those
are what make the speaker merge accurate — segment-level timing is too coarse,
since a Whisper segment routinely spans a speaker change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def transcribe(wav_path: Path, model: str, language: str | None = None) -> dict[str, Any]:
    import mlx_whisper  # imported lazily so the CLI works without MLX installed

    kwargs: dict[str, Any] = {
        "path_or_hf_repo": model,
        "word_timestamps": True,
        "condition_on_previous_text": False,  # limits runaway hallucination loops
    }
    if language:
        kwargs["language"] = language

    return mlx_whisper.transcribe(str(wav_path), **kwargs)


def flatten_words(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull a flat, time-ordered word list out of Whisper's nested output.

    Falls back to emitting whole segments as pseudo-words if word timestamps are
    missing, so the merge stage always has something to work with.
    """
    words: list[dict[str, Any]] = []
    for seg in result.get("segments", []):
        seg_words = seg.get("words")
        if seg_words:
            for w in seg_words:
                start, end = w.get("start"), w.get("end")
                text = (w.get("word") or w.get("text") or "").strip()
                if start is None or end is None or not text:
                    continue
                words.append({"start": float(start), "end": float(end), "text": text})
        else:
            text = (seg.get("text") or "").strip()
            if text and seg.get("start") is not None:
                words.append(
                    {
                        "start": float(seg["start"]),
                        "end": float(seg.get("end", seg["start"])),
                        "text": text,
                    }
                )
    words.sort(key=lambda w: (w["start"], w["end"]))
    return words
