"""Plain data returned by the Ripperr API.

Nothing here knows how or where anything is stored, so callers (the CLI, another
service) don't change if the storage backend does.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    id: int
    url: str
    title: str | None


@dataclass(frozen=True)
class Episode:
    id: int  # local to this instance; use `guid` to refer to an episode elsewhere
    guid: str  # the stable public key, derived from the feed and its own id for the episode
    source_guid: str  # the feed's own id for the episode; only unique within that feed
    feed_id: int
    title: str | None
    published: str | None
    audio_url: str
    source_url: str | None  # public RSS entry or YouTube watch URL
    audio_path: str | None  # None until downloaded, and again once the file is deleted
    duration: float | None
    status: str  # new | downloaded | done | error
    error: str | None
    updated_at: str  # ISO 8601 UTC
    revision: int  # bumped whenever the transcript is rewritten (remerge, glossary change)
    merged_at: str | None


@dataclass(frozen=True)
class Turn:
    idx: int
    speaker: str
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Correction:
    heard: str
    fixed: str
    count: int


@dataclass(frozen=True)
class Hit:
    episode_id: int
    guid: str
    episode_title: str | None
    speaker: str
    start: float
    end: float
    snippet: str


@dataclass(frozen=True)
class Transcript:
    episode: Episode
    turns: list[Turn]
    corrections: list[Correction]  # glossary swaps applied to this revision
