"""Configuration: paths and processing settings.

Everything is overridable from the environment so you can point the pipeline at
an external drive without editing code.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

_CACHE_FORMAT_VERSION = 2


def stage_key(config: dict[str, object]) -> str:
    """Short, stable id for one model configuration of an ASR or diarization run.
    Raw model output is cached under it, so two models never share a cache file."""
    payload = {**config, "version": _CACHE_FORMAT_VERSION}
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()[:12]


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


@dataclass
class Config:
    root: Path = field(default_factory=lambda: _env_path("RIPPERR_ROOT", Path.home() / "ripperr"))

    # Optional text-side guest and ad classification. DeepInfra exposes an
    # OpenAI-compatible endpoint, so this stays a small requests-based integration.
    deepinfra_token: str | None = field(default_factory=lambda: (
        os.environ.get("RIPPERR_DEEPINFRA_TOKEN") or os.environ.get("DEEPINFRA_TOKEN")
    ))
    deepinfra_model: str = field(default_factory=lambda: os.environ.get(
        "RIPPERR_DEEPINFRA_MODEL", "deepseek-ai/DeepSeek-V4-Flash-0731"
    ))
    deepinfra_base_url: str = field(default_factory=lambda: os.environ.get(
        "RIPPERR_DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai"
    ).rstrip("/"))
    auto_classify_ads: bool = field(default_factory=lambda: os.environ.get(
        "RIPPERR_AUTO_CLASSIFY_ADS", "0"
    ) == "1")

    # Turn segmentation
    max_turn_gap: float = 2.0  # seconds of silence that forces a new turn
    orphan_word_gap: float = 0.5  # how far a word may reach for a neighbouring speaker

    # Phonetic similarity (0-1) needed to respell a heard name as a glossary term.
    glossary_match: float = 0.95

    keep_audio: bool = field(default_factory=lambda: os.environ.get("RIPPERR_KEEP_AUDIO", "1") != "0")

    # A built dashboard (`npm run build` in frontend/) to serve next to the API.
    # The files hold no secrets; the API still needs the token.
    dashboard_dir: Path | None = field(default_factory=lambda: (
        Path(os.environ["RIPPERR_DASHBOARD_DIR"]).expanduser() if os.environ.get("RIPPERR_DASHBOARD_DIR") else None
    ))

    # Remote workers (docs/worker-contract.md). A claimed episode returns to the
    # queue once its lease expires. Speaker vectors from any other embedding
    # space are dropped: vectors from different models are not comparable.
    lease_seconds: int = field(default_factory=lambda: int(os.environ.get("RIPPERR_LEASE_SECONDS", "7200")))
    embedding_space: str = field(default_factory=lambda: os.environ.get(
        "RIPPERR_EMBEDDING_SPACE", "senko-campplus"
    ))

    @property
    def db_path(self) -> Path:
        return self.root / "ripperr.db"

    @property
    def glossary_path(self) -> Path:
        """Default glossary file, one term per line. Optional; callers using the
        Python API normally pass terms directly instead."""
        return _env_path("RIPPERR_GLOSSARY", self.root / "glossary.txt")

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def raw_dir(self) -> Path:
        """Unmerged ASR and diarization output, kept so you can re-merge without
        re-running the models."""
        return self.root / "raw"

    def raw_path(self, guid: str, kind: str, key: str) -> Path:
        """Cache path keyed by episode and by the `stage_key` of the model run
        that produced the output."""
        if kind not in {"asr", "diar", "embed"}:
            raise ValueError("cache kind must be asr, diar, or embed")
        return self.raw_dir / f"{self.episode_key(guid)}.{kind}-{key}.json"

    @staticmethod
    def episode_key(guid: str) -> str:
        return hashlib.sha1(guid.encode()).hexdigest()[:12]

    def ensure_dirs(self) -> None:
        for d in (self.root, self.audio_dir, self.raw_dir):
            d.mkdir(parents=True, exist_ok=True)


def default_config() -> Config:
    """Build the process default only when a caller actually needs it."""
    return Config()
