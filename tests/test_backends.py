from types import SimpleNamespace

import pytest

from ripperr import asr, diarize


def test_auto_selects_linux_backends(monkeypatch):
    monkeypatch.setattr(asr.platform, "system", lambda: "Linux")
    monkeypatch.setattr(asr.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(diarize.platform, "system", lambda: "Linux")
    monkeypatch.setattr(diarize.platform, "machine", lambda: "x86_64")

    assert asr.backend_name() == "faster-whisper"
    assert diarize.backend_name() == "pyannote"


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
