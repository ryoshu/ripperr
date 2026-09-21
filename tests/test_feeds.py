from contextlib import contextmanager

import pytest

from ripperr import feeds


class FakeResponse:
    headers = {"Content-Length": str(feeds.MAX_DOWNLOAD_BYTES + 1)}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        pass


def test_download_rejects_oversized_content_before_writing(tmp_path, monkeypatch):
    @contextmanager
    def fake_get(*_args, **_kwargs):
        yield FakeResponse()

    monkeypatch.setattr(feeds.requests, "get", fake_get)
    with pytest.raises(RuntimeError, match="1 GiB"):
        feeds.download("https://example.com/audio.mp3", tmp_path, "Episode")
    assert list(tmp_path.iterdir()) == []
