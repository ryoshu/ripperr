from contextlib import contextmanager

from ripperr import youtube


class FakeYoutubeDL:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        assert download is False
        if "watch?v=" in _url:
            return {"timestamp": 1735732800, "description": "Full notes"}
        return {
            "title": "Show",
            "entries": [
                {"id": "one", "title": "One", "url": "audio-one", "description": "Notes", "timestamp": 1735732800},
                {"id": "two", "title": "Two", "url": "audio-two", "upload_date": "20260102"},
            ],
        }


def test_playlist_sync_uses_flat_metadata_without_page_fetch(monkeypatch):
    @contextmanager
    def fake_ydl(**_opts):
        yield FakeYoutubeDL()

    monkeypatch.setattr(youtube, "_ydl", fake_ydl)
    _, episodes = youtube.parse_playlist(
        "https://youtube.com/playlist?list=test",
        {"yt:one"},
        {"yt:two"},
    )

    assert episodes[0]["summary"] == "Notes"
    assert episodes[1]["summary"] is None
    assert episodes[0]["published"] == "2025-01-01T12:00:00+00:00"
    assert episodes[1]["published"] == "2026-01-02T00:00:00+00:00"


def test_youtube_detection_requires_a_youtube_hostname():
    assert youtube.is_youtube("https://www.youtube.com/playlist?list=test")
    assert youtube.is_youtube("https://youtu.be/video")
    assert not youtube.is_youtube("https://evil.example/youtube.com/playlist")
    assert not youtube.is_youtube("https://youtube.com.evil.example/playlist")


def test_playlist_sync_fetches_full_metadata_when_flat_entry_has_no_date(monkeypatch):
    @contextmanager
    def fake_ydl(**_opts):
        yield FakeYoutubeDLWithoutDates()

    monkeypatch.setattr(youtube, "_ydl", fake_ydl)
    _, episodes = youtube.parse_playlist(
        "https://youtube.com/playlist?list=test",
        {"yt:one"},
        {"yt:two"},
    )

    assert episodes[0]["published"] == "2025-01-01T12:00:00+00:00"
    assert episodes[0]["summary"] == "Full notes"
    assert episodes[1]["published"] == "2025-01-01T12:00:00+00:00"


class FakeYoutubeDLWithoutDates(FakeYoutubeDL):
    def extract_info(self, url, download=False):
        result = super().extract_info(url, download)
        if "watch?v=" not in url:
            for entry in result["entries"]:
                entry.pop("timestamp", None)
                entry.pop("upload_date", None)
                entry.pop("description", None)
        return result
