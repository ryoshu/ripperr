from contextlib import contextmanager
from types import SimpleNamespace

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


def test_parse_feed_extracts_episode_metadata(monkeypatch):
    entry = SimpleNamespace(
        id="episode-1",
        title="Episode 1",
        summary="A summary",
        published_parsed=(2026, 9, 21, 12, 30, 0),
        enclosures=[{"href": "https://cdn.example/episode.mp3", "type": "audio/mpeg"}],
        links=[{"rel": "alternate", "href": "https://example.com/episode-1"}],
        link="https://example.com/episode-1",
    )
    parsed = SimpleNamespace(
        bozo=False,
        entries=[entry],
        feed=SimpleNamespace(title="Show"),
    )
    monkeypatch.setattr(feeds.feedparser, "parse", lambda *_args, **_kwargs: parsed)

    title, episodes = feeds.parse_feed("https://example.com/feed.xml")

    assert title == "Show"
    assert episodes == [{
        "source_guid": "episode-1",
        "title": "Episode 1",
        "summary": "A summary",
        "published": "2026-09-21T12:30:00+00:00",
        "audio_url": "https://cdn.example/episode.mp3",
        "source_url": "https://example.com/episode-1",
    }]
