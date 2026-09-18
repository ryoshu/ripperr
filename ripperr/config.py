"""Configuration: paths and model choices.

Everything is overridable from the environment so you can point the pipeline at
an external drive without editing code.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


@dataclass
class Config:
    root: Path = field(default_factory=lambda: _env_path("RIPPERR_ROOT", Path.home() / "ripperr"))

    # ASR. Turbo is the sane default on Apple Silicon: near-large-v3 quality at a
    # fraction of the runtime. Swap for mlx-community/whisper-large-v3-mlx if you
    # care more about accuracy than throughput.
    asr_model: str = field(
        default_factory=lambda: os.environ.get(
            "RIPPERR_ASR_MODEL", "mlx-community/whisper-large-v3-turbo"
        )
    )
    language: str | None = field(default_factory=lambda: os.environ.get("RIPPERR_LANGUAGE") or None)

    # Turn segmentation
    max_turn_gap: float = 2.0  # seconds of silence that forces a new turn
    orphan_word_gap: float = 0.5  # how far a word may reach for a neighbouring speaker

    # Phonetic similarity (0-1) needed to respell a heard name as a glossary term.
    glossary_match: float = 0.95

    keep_audio: bool = field(default_factory=lambda: os.environ.get("RIPPERR_KEEP_AUDIO", "1") != "0")

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

    def raw_path(self, guid: str, kind: str) -> Path:
        """Cache file for one stage's output (kind is "asr" or "diar"). Keyed by a
        hash of the guid rather than a database id, so it survives a storage swap."""
        return self.raw_dir / f"{hashlib.sha1(guid.encode()).hexdigest()[:12]}.{kind}.json"

    def ensure_dirs(self) -> None:
        for d in (self.root, self.audio_dir, self.raw_dir):
            d.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
