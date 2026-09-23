"""Configuration: paths and model choices.

Everything is overridable from the environment so you can point the pipeline at
an external drive without editing code.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_CACHE_FORMAT_VERSION = 2


def _runtime_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}


def resolve_asr_backend(backend: str = "auto") -> str:
    if backend == "auto":
        return "mlx" if is_apple_silicon() else "faster-whisper"
    if backend not in {"mlx", "faster-whisper"}:
        raise ValueError("RIPPERR_ASR_BACKEND must be auto, mlx, or faster-whisper")
    return backend


def resolve_diarization_backend(backend: str = "auto") -> str:
    if backend == "auto":
        return "senko" if is_apple_silicon() else "pyannote"
    if backend not in {"senko", "pyannote"}:
        raise ValueError("RIPPERR_DIARIZATION_BACKEND must be auto, senko, or pyannote")
    return backend


def _default_asr_model(backend: str) -> str:
    return "mlx-community/whisper-large-v3-turbo" if backend == "mlx" else "large-v3"


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

    # Optional text-side guest extraction. DeepInfra exposes an OpenAI-compatible
    # endpoint, so this stays a small requests-based integration.
    deepinfra_token: str | None = field(default_factory=lambda: (
        os.environ.get("RIPPERR_DEEPINFRA_TOKEN") or os.environ.get("DEEPINFRA_TOKEN")
    ))
    deepinfra_model: str = field(default_factory=lambda: os.environ.get(
        "RIPPERR_DEEPINFRA_MODEL", "deepseek-ai/DeepSeek-V4-Flash-0731"
    ))
    deepinfra_base_url: str = field(default_factory=lambda: os.environ.get(
        "RIPPERR_DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai"
    ).rstrip("/"))

    # Turn segmentation
    max_turn_gap: float = 2.0  # seconds of silence that forces a new turn
    orphan_word_gap: float = 0.5  # how far a word may reach for a neighbouring speaker

    # Phonetic similarity (0-1) needed to respell a heard name as a glossary term.
    glossary_match: float = 0.95

    keep_audio: bool = field(default_factory=lambda: os.environ.get("RIPPERR_KEEP_AUDIO", "1") != "0")

    def __post_init__(self) -> None:
        self.asr_backend = resolve_asr_backend(self.asr_backend)
        self.diarization_backend = resolve_diarization_backend(self.diarization_backend)
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
        """Cache path keyed by episode and the model configuration for the stage."""
        if kind not in {"asr", "diar", "embed"}:
            raise ValueError("cache kind must be asr, diar, or embed")
        episode_key = self.episode_key(guid)
        config = self._cache_config(kind)
        config_key = hashlib.sha256(json.dumps(
            config, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()[:12]
        return self.raw_dir / f"{episode_key}.{kind}-{config_key}.json"

    @staticmethod
    def episode_key(guid: str) -> str:
        return hashlib.sha1(guid.encode()).hexdigest()[:12]

    def _cache_config(self, kind: str) -> dict[str, object]:
        if kind == "asr":
            backend = self.asr_backend
            return {
                "version": _CACHE_FORMAT_VERSION,
                "backend": backend,
                "runtime": _runtime_version(
                    "mlx-whisper" if backend == "mlx" else "faster-whisper"
                ),
                "model": self.asr_model,
                "device": self.device,
                "language": self.language,
            }
        backend = self.diarization_backend
        return {
            "version": _CACHE_FORMAT_VERSION,
            "backend": backend,
            "runtime": _runtime_version("senko" if backend == "senko" else "pyannote-audio"),
            "model": self.diarization_model,
            "device": self.device,
        }

    def ensure_dirs(self) -> None:
        for d in (self.root, self.audio_dir, self.raw_dir):
            d.mkdir(parents=True, exist_ok=True)


def default_config() -> Config:
    """Build the process default only when a caller actually needs it."""
    return Config()
