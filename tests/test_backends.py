from types import SimpleNamespace
import os
import subprocess
import sys

import pytest

from ripperr import asr, diarize
from ripperr import config


def test_auto_selects_linux_backends(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config.platform, "machine", lambda: "x86_64")

    assert asr.backend_name() == "faster-whisper"
    assert diarize.backend_name() == "pyannote"


def test_config_resolves_auto_once(monkeypatch, tmp_path):
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")

    cfg = config.Config(root=tmp_path)

    assert cfg.asr_backend == "mlx"
    assert cfg.diarization_backend == "senko"
    assert cfg.asr_model == "mlx-community/whisper-large-v3-turbo"


def test_faster_whisper_segments_match_merge_contract():
    segment = SimpleNamespace(
        start=1,
        end=3,
        text=" hello",
        words=[SimpleNamespace(start=1, end=2, word=" hello")],
    )

    assert asr._faster_segment(segment) == {
        "start": 1.0,
        "end": 3.0,
        "text": " hello",
        "words": [{"start": 1.0, "end": 2.0, "word": " hello"}],
    }


def test_unknown_backend_is_explicit():
    with pytest.raises(ValueError, match="RIPPERR_ASR_BACKEND"):
        asr.backend_name("nope")
    with pytest.raises(ValueError, match="RIPPERR_DIARIZATION_BACKEND"):
        diarize.backend_name("nope")


def test_invalid_environment_is_deferred_until_config_creation():
    env = os.environ.copy()
    env["RIPPERR_ASR_BACKEND"] = "bogus"
    result = subprocess.run(
        [sys.executable, "-c", "import ripperr.cli"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0

    result = subprocess.run(
        [sys.executable, "-c", "from ripperr.config import default_config; default_config()"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and "RIPPERR_ASR_BACKEND" in result.stderr
