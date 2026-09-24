"""Small DeepInfra client for text-side identity evidence."""

from __future__ import annotations

import json
from collections.abc import Iterable

import requests

from .models import Episode, GuestHint, Turn


class DeepInfraError(RuntimeError):
    """DeepInfra returned an unusable response."""


def guest_hints(
    episode: Episode,
    turns: Iterable[Turn],
    *,
    token: str,
    model: str,
    base_url: str,
    host_name: str | None = None,
    existing_names: set[str] | None = None,
) -> list[GuestHint]:
    """Ask DeepInfra for explicit guest candidates from episode context."""
    opening_turns = [
        {"speaker": turn.speaker, "text": turn.text[:1000]}
        for turn in list(turns)[:12]
    ]
    context = {
        "title": episode.title or "",
        "summary": (episode.summary or "")[:5000],
        "opening_turns": opening_turns,
        "known_host": host_name or "",
    }
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You identify likely podcast guests from episode metadata and "
                            "the opening transcript. Return only a JSON object with a "
                            "guests array. Include a person only when the text explicitly "
                            "introduces them as a guest, interview subject, or co-host. "
                            "Do not return people merely mentioned as news or examples. "
                            "Exclude the known host. Each item must have name and a short "
                            "evidence quote copied from the input. Return an empty array "
                            "when the evidence is unclear; do not guess."
                        ),
                    },
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": 300,
            },
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("message.content must be a string")
        parsed = json.loads(_strip_json_fence(content))
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        raise DeepInfraError(f"guest extraction failed: {exc}") from exc

    guests = parsed.get("guests") if isinstance(parsed, dict) else None
    if not isinstance(guests, list):
        raise DeepInfraError("guest extraction returned no guests array")

    seen = {name.casefold() for name in (existing_names or set())}
    if host_name:
        seen.add(host_name.casefold())
    evidence_sources = [context["title"], context["summary"]]
    evidence_sources.extend(turn["text"] for turn in opening_turns)
    hints: list[GuestHint] = []
    for item in guests:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = " ".join(item["name"].split()).strip(" \t\n-:;,.!?()[]")
        key = name.casefold()
        if not name or len(name) > 120 or key in seen:
            continue
        evidence = item.get("evidence")
        if (
            not isinstance(evidence, str)
            or not evidence
            or len(evidence) > 240
            or not any(evidence in source for source in evidence_sources)
            or key not in evidence.casefold()
        ):
            continue
        seen.add(key)
        hints.append(GuestHint(episode.guid, name, "llm", evidence, 0.70))
    return hints


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    return text
