"""Configuration: paths and model choices.

Everything is overridable from the environment so you can point the pipeline at
an external drive without editing code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


@dataclass
class Config:
    root: Path = field(default_factory=lambda: _env_path("PODPIPE_ROOT", Path.home() / "podpipe"))

    # ASR. Turbo is the sane default on Apple Silicon: near-large-v3 quality at a
    # fraction of the runtime. Swap for mlx-community/whisper-large-v3-mlx if you
    # care more about accuracy than throughput.
    asr_model: str = field(
        default_factory=lambda: os.environ.get(
            "PODPIPE_ASR_MODEL", "mlx-community/whisper-large-v3-turbo"
        )
    )
    language: str | None = field(default_factory=lambda: os.environ.get("PODPIPE_LANGUAGE") or None)

    # Turn segmentation
    max_turn_gap: float = 2.0  # seconds of silence that forces a new turn
    orphan_word_gap: float = 0.5  # how far a word may reach for a neighbouring speaker

    # Phonetic similarity (0-1) needed to respell a heard name as a roster name.
    name_match: float = 0.95

    # Opt in to YouTube auto-captions instead of Whisper. Off by default: on a
    # fantasy-football episode they agreed with Whisper on ~95% of tokens but
    # mangled player names (Bijan -> "Bejian"), which is what you search for.
    use_captions: bool = field(default_factory=lambda: os.environ.get("PODPIPE_YT_CAPTIONS", "0") == "1")

    keep_audio: bool = field(default_factory=lambda: os.environ.get("PODPIPE_KEEP_AUDIO", "1") != "0")

    @property
    def db_path(self) -> Path:
        return self.root / "podpipe.db"

    @property
    def players_path(self) -> Path:
        """Roster, one name per line. Built by `podpipe players`; optional."""
        return self.root / "players.txt"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def raw_dir(self) -> Path:
        """Unmerged ASR and diarization output, kept so you can re-merge without
        re-running the models."""
        return self.root / "raw"

    def ensure_dirs(self) -> None:
        for d in (self.root, self.audio_dir, self.raw_dir):
            d.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
