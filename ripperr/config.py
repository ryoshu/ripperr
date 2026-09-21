"""Configuration: paths and model choices.

Everything is overridable from the environment so you can point the pipeline at
an external drive without editing code.
"""

from __future__ import annotations

import hashlib
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _default_asr_model(backend: str | None = None) -> str:
    backend = backend or os.environ.get("RIPPERR_ASR_BACKEND", "auto")
    apple = platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}
    return "mlx-community/whisper-large-v3-turbo" if backend == "mlx" or (backend == "auto" and apple) else "large-v3"


@dataclass
class Config:
    root: Path = field(default_factory=lambda: _env_path("RIPPERR_ROOT", Path.home() / "ripperr"))

    # Processing backends. `auto` keeps the Apple path on Apple Silicon and
    # selects the Linux adapters everywhere else.
    asr_backend: str = field(default_factory=lambda: os.environ.get("RIPPERR_ASR_BACKEND", "auto"))
    diarization_backend: str = field(
        default_factory=lambda: os.environ.get("RIPPERR_DIARIZATION_BACKEND", "auto")
    )
    device: str = field(default_factory=lambda: os.environ.get("RIPPERR_DEVICE", "auto"))

    # ASR. Turbo is the sane default on Apple Silicon: near-large-v3 quality at a
    # fraction of the runtime. Swap for mlx-community/whisper-large-v3-mlx if you
    # care more about accuracy than throughput.
    asr_model: str | None = field(default_factory=lambda: os.environ.get("RIPPERR_ASR_MODEL"))
    diarization_model: str = field(default_factory=lambda: os.environ.get(
        "RIPPERR_DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1"
    ))
    diarization_token: str | None = field(default_factory=lambda: (
        os.environ.get("RIPPERR_HF_TOKEN") or os.environ.get("HF_TOKEN")
    ))
    language: str | None = field(default_factory=lambda: os.environ.get("RIPPERR_LANGUAGE") or None)

    # Turn segmentation
    max_turn_gap: float = 2.0  # seconds of silence that forces a new turn
    orphan_word_gap: float = 0.5  # how far a word may reach for a neighbouring speaker

    # Phonetic similarity (0-1) needed to respell a heard name as a glossary term.
    glossary_match: float = 0.95

    keep_audio: bool = field(default_factory=lambda: os.environ.get("RIPPERR_KEEP_AUDIO", "1") != "0")

    def __post_init__(self) -> None:
        if self.asr_model is None:
            self.asr_model = _default_asr_model(self.asr_backend)

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
