"""Conservative text evidence used alongside voice identity matching."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import Episode, GuestHint, Turn


_NAME = r"[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,3}"
_NAME_LIST = rf"{_NAME}(?:(?:\s+and\s+|,\s*(?:and\s+)?){_NAME})*"
_METADATA_CONTEXT = re.compile(
    rf"\b(?:with|w/|joined by|featuring|feat\.?|talking to|interview with|welcomes)\s+"
    rf"(?P<names>{_NAME_LIST})",
)
_TRANSCRIPT_CONTEXT = re.compile(
    rf"\b(?:joined by|welcomes?|featuring|interview with|talking (?:to|with)|special guest)\s+"
    rf"(?P<names>{_NAME_LIST})",
)
_TRAILING_NAME_WORDS = {"like", "that", "so", "the", "to", "for", "is", "are", "you"}


def guest_hints(
    episode: Episode,
    turns: Iterable[Turn],
    *,
    host_name: str | None = None,
    max_turns: int = 12,
) -> list[GuestHint]:
    """Return unique guest candidates with the exact text that produced them.

    The matcher intentionally only trusts explicit introduction phrases. It does
    not infer a person's identity from their voice, and it does not claim the
    returned confidence is a statistical probability.
    """
    host_key = _name_key(host_name) if host_name else None
    first_turns = list(turns)[:max_turns]
    sources = [
        ("title", episode.title or "", 0.95),
        ("summary", episode.summary or "", 0.85),
        ("transcript", " ".join(turn.text for turn in first_turns), 0.75),
    ]
    hints: list[GuestHint] = []
    seen: set[str] = set()
    for source, text, confidence in sources:
        pattern = _TRANSCRIPT_CONTEXT if source == "transcript" else _METADATA_CONTEXT
        for match in pattern.finditer(text):
            evidence = match.group(0).strip(" \t\n-:;,.!?()[]")
            for candidate in re.split(r"\s+and\s+|,\s*(?:and\s+)?", match.group("names")):
                name = _clean_name(candidate)
                key = _name_key(name)
                if not key or key in seen or key == host_key:
                    continue
                seen.add(key)
                hints.append(GuestHint(episode.guid, name, source, evidence, confidence))
    return hints


def _name_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _clean_name(value: str) -> str:
    words = value.strip(" \t\n-:;,.!?()[]").split()
    while words and words[-1].casefold().strip(".,!?;:") in _TRAILING_NAME_WORDS:
        words.pop()
    if words and words[-1].endswith(("'s", "’s")):
        words[-1] = words[-1][:-2]
    return " ".join(words).strip(" \t\n-:;,.!?()[]")
