from contextlib import contextmanager

from ripperr import youtube


class FakeYoutubeDL:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        assert download is False
        return {
            "title": "Show",
            "entries": [
                {"id": "one", "title": "One", "url": "audio-one", "description": "Notes"},
                {"id": "two", "title": "Two", "url": "audio-two"},
            ],
        }


def test_playlist_sync_uses_flat_metadata_without_page_fetch(monkeypatch):
    @contextmanager
    def fake_ydl(**_opts):
        yield FakeYoutubeDL()

    monkeypatch.setattr(youtube, "_ydl", fake_ydl)
    _, episodes = youtube.parse_playlist("https://youtube.com/playlist?list=test")

    assert episodes[0]["summary"] == "Notes"
    assert episodes[1]["summary"] is None
