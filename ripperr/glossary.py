"""Respell misheard proper names in ASR output using a glossary of known terms.

Whisper spells names the way they sound ("Basial Tootin" for Bhayshul Tuten), so
we compare each capitalised word pair to the glossary by phonetic key (metaphone)
and swap in the glossary spelling when it's a near-exact sound match. Only
two-word terms (first + last name) are matched, and every swap is reported, so a
false positive shows up in the corrections list instead of silently rewriting the
transcript.

The glossary is domain data supplied by the caller; nothing here knows about any
particular subject.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import jellyfish as jf

# core word, then an optional possessive and trailing punctuation
_TOKEN = re.compile(r"^(?P<core>[A-Za-z][A-Za-z'’\-]*?)(?P<tail>(?:['’]s)?\W*)$")


def load(path: Path) -> list[str]:
    """Terms from a text file, one per line; blank lines and #-comments ignored."""
    if not path.exists():
        return []
    lines = (ln.strip() for ln in path.read_text().splitlines())
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]", "", s.lower())


def _key(s: str) -> str:
    return "".join(jf.metaphone(w) for w in _norm(s).split())


def correct(
    words: list[dict[str, Any]], terms: list[str], threshold: float = 0.95
) -> tuple[list[dict[str, Any]], Counter]:
    """Return (words with names respelled, Counter of (heard, fixed))."""
    known = {_norm(n) for n in terms}
    keys = [(n, _key(n)) for n in terms]
    best: dict[str, str | None] = {}  # heard -> glossary term, cached per distinct pair
    fixes: Counter = Counter()
    out: list[dict[str, Any]] = []

    i = 0
    while i < len(words):
        a = words[i]
        b = words[i + 1] if i + 1 < len(words) else None
        ma = _TOKEN.match(a["text"])
        mb = _TOKEN.match(b["text"]) if b else None
        if (
            ma and mb
            and not ma["tail"]
            and ma["core"][0].isupper() and mb["core"][0].isupper()
            and len(ma["core"]) > 2 and len(mb["core"]) > 2
        ):
            # "And Michael Penix": don't let a stray word eat the start of a real name
            mc = _TOKEN.match(words[i + 2]["text"]) if i + 2 < len(words) else None
            if mc and f"{_norm(mb['core'])} {_norm(mc['core'])}" in known:
                out.append(a)
                i += 1
                continue
            heard = f"{ma['core']} {mb['core']}"
            if heard not in best:
                best[heard] = None
                if _norm(heard) not in known:
                    ck = _key(heard)
                    name, score = max(
                        ((n, jf.jaro_winkler_similarity(ck, k)) for n, k in keys),
                        key=lambda x: x[1],
                    )
                    if score >= threshold:
                        best[heard] = name
            name = best[heard]
            if name:
                # one word spanning both originals keeps the timing for the merge
                out.append({**a, "text": name + mb["tail"], "end": b["end"]})
                fixes[(heard, name)] += 1
                i += 2
                continue
        out.append(a)
        i += 1
    return out, fixes
