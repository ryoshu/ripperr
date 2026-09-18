"""Reconciling ASR words with diarization segments.

This is the part that actually determines transcript quality, and the part every
tutorial hand-waves. The two models segment the audio independently: Whisper's
boundaries follow linguistic units, the diarizer's follow acoustic ones, and they
disagree constantly — especially at turn boundaries, which is exactly where
getting it wrong is most visible.

The approach: assign each word to whichever speaker segment it overlaps most, in
time. Words that overlap nothing (breaths between turns, words the VAD clipped)
inherit from a close neighbour. Then collapse runs of same-speaker words into
turns, splitting on long pauses.
"""

from __future__ import annotations

from bisect import bisect_left
from typing import Any

UNKNOWN = "SPEAKER_?"


def assign_speakers(
    words: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    orphan_gap: float = 0.5,
) -> list[dict[str, Any]]:
    """Attach a `speaker` key to every word. Input must be sorted by start."""
    if not segments:
        return [{**w, "speaker": UNKNOWN} for w in words]

    starts = [s["start"] for s in segments]
    ends = [s["end"] for s in segments]
    max_len = max(e - s for s, e in zip(starts, ends)) or 0.0

    out: list[dict[str, Any]] = []
    for w in words:
        out.append({**w, "speaker": _best_speaker(w, segments, starts, max_len, orphan_gap)})

    _fill_orphans(out, orphan_gap)
    return out


def _best_speaker(
    word: dict[str, Any],
    segments: list[dict[str, Any]],
    starts: list[float],
    max_len: float,
    orphan_gap: float,
) -> str:
    ws, we = word["start"], word["end"]

    # Candidate window: any segment starting early enough that it could still be
    # running when the word begins, through any starting before the word ends.
    lo = max(0, bisect_left(starts, ws - max_len - orphan_gap) - 1)
    hi = bisect_left(starts, we + orphan_gap)

    best_speaker, best_overlap = None, 0.0
    nearest_speaker, nearest_dist = None, float("inf")

    for seg in segments[lo:hi + 1]:
        overlap = min(we, seg["end"]) - max(ws, seg["start"])
        if overlap > best_overlap:
            best_overlap, best_speaker = overlap, seg["speaker"]
        if overlap <= 0:
            dist = seg["start"] - we if seg["start"] > we else ws - seg["end"]
            if 0 <= dist < nearest_dist:
                nearest_dist, nearest_speaker = dist, seg["speaker"]

    if best_speaker is not None:
        return best_speaker
    if nearest_speaker is not None and nearest_dist <= orphan_gap:
        return nearest_speaker
    return UNKNOWN


def _fill_orphans(words: list[dict[str, Any]], orphan_gap: float) -> None:
    """Words still unattributed take the previous speaker if it's adjacent enough,
    otherwise the next one. Modifies in place."""
    for i, w in enumerate(words):
        if w["speaker"] != UNKNOWN:
            continue
        prev = next((x for x in reversed(words[:i]) if x["speaker"] != UNKNOWN), None)
        nxt = next((x for x in words[i + 1:] if x["speaker"] != UNKNOWN), None)
        prev_gap = w["start"] - prev["end"] if prev else float("inf")
        next_gap = nxt["start"] - w["end"] if nxt else float("inf")
        if min(prev_gap, next_gap) > orphan_gap * 4:
            continue
        w["speaker"] = prev["speaker"] if prev_gap <= next_gap else nxt["speaker"]


def group_turns(
    words: list[dict[str, Any]], max_gap: float = 2.0
) -> list[dict[str, Any]]:
    """Collapse consecutive same-speaker words into turns."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for w in words:
        new_turn = (
            current is None
            or w["speaker"] != current["speaker"]
            or w["start"] - current["end"] > max_gap
        )
        if new_turn:
            if current:
                turns.append(_finish(current))
            current = {
                "speaker": w["speaker"],
                "start": w["start"],
                "end": w["end"],
                "parts": [w["text"]],
            }
        else:
            current["end"] = w["end"]
            current["parts"].append(w["text"])

    if current:
        turns.append(_finish(current))
    return turns


def _finish(turn: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(turn.pop("parts"))
    # Whisper word tokens carry their own leading spaces and punctuation; tidy up.
    for punct in (" ,", " .", " ?", " !", " ;", " :", " '", " n't", " %"):
        text = text.replace(punct, punct.strip())
    return {**turn, "text": " ".join(text.split())}


def merge(
    words: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    max_gap: float = 2.0,
    orphan_gap: float = 0.5,
) -> list[dict[str, Any]]:
    return group_turns(assign_speakers(words, segments, orphan_gap), max_gap)
