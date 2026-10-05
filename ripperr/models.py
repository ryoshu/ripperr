"""Plain data returned by the Ripperr API.

Nothing here knows how or where anything is stored, so callers (the CLI, another
service) don't change if the storage backend does.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    summary: str | None
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
class SpeakerName:
    episode_guid: str
    speaker: str
    name: str
    method: str  # manual | voice_match | transcript_hint
    confidence: float | None
    updated_at: str  # ISO 8601 UTC


@dataclass(frozen=True)
class SpeakerEmbedding:
    episode_guid: str
    speaker: str
    embedding: tuple[float, ...]
    updated_at: str  # ISO 8601 UTC


@dataclass(frozen=True)
class SpeakerProfile:
    feed_id: int
    name: str
    embedding: tuple[float, ...]
    sample_count: int
    updated_at: str  # ISO 8601 UTC


@dataclass(frozen=True)
class SpeakerMatch:
    episode_guid: str
    speaker: str
    name: str
    score: float
    sample_count: int


@dataclass(frozen=True)
class GuestHint:
    episode_guid: str
    name: str
    source: str  # title | summary | transcript | llm
    evidence: str
    confidence: float


@dataclass(frozen=True)
class AdSpan:
    episode_guid: str
    start: float
    end: float
    category: str  # sponsor | self_promotion | affiliate | crowdfunding
    confidence: float
    evidence: str
    detector: str


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
    speaker_names: list[SpeakerName] = field(default_factory=list)
    ad_spans: list[AdSpan] = field(default_factory=list)
    ads_checked: bool = False


@dataclass(frozen=True)
class EpisodeDetail:
    transcript: Transcript
    feed: Feed
    speaker_matches: list[SpeakerMatch]
    guest_hints: list[GuestHint]


@dataclass(frozen=True)
class Worker:
    name: str
    created_at: str  # ISO 8601 UTC
    last_seen: str | None  # last authenticated /v1/work request; None if never
    lease_guid: str | None  # episode it holds an unexpired lease on, if any


@dataclass(frozen=True)
class Change:
    seq: int
    episode_guid: str
    revision: int
    kind: str  # transcript | metadata | deleted
    occurred_at: str  # ISO 8601 UTC
