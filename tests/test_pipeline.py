from pathlib import Path

from ripperr.config import Config
from ripperr.pipeline import Processor
from ripperr.store import Store


def test_failed_episode_can_be_retried_and_completed(tmp_path, monkeypatch):
    cfg = Config(root=tmp_path)
    store = Store(cfg.db_path)
    feed = store.add_feed("http://feed")
    store.add_episode(feed.id, "one", "One", None, "http://audio")
    processor = Processor(cfg, log=lambda _: None)
    failed = True

    monkeypatch.setattr(processor, "_ensure_audio", lambda _store, _episode: Path("one.wav"))

    def transcribe(_episode, _wav, _force):
        nonlocal failed
        if failed:
            failed = False
            raise RuntimeError("temporary model failure")
        return {"segments": [{"words": [{"start": 0, "end": 1, "word": "hello"}]}]}

    monkeypatch.setattr(processor, "_ensure_asr", transcribe)
    monkeypatch.setattr(
        processor,
        "_ensure_diarization",
        lambda _episode, _wav, _force: [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}],
    )

    processor.process(store, store.episode_by_id(1))
    assert store.episode_by_id(1).status == "error"

    processor.process(store, store.episode_by_id(1))
    episode = store.episode_by_id(1)
    assert (episode.status, episode.error, episode.revision) == ("done", None, 1)
    store.close()


def test_senko_embeddings_are_cached_and_stored(tmp_path, monkeypatch):
    cfg = Config(root=tmp_path)
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    feed = store.add_feed("http://feed")
    store.add_episode(feed.id, "one", "One", None, "http://audio")
    processor = Processor(cfg, log=lambda _: None)

    monkeypatch.setattr(processor, "_ensure_audio", lambda _store, _episode: Path("one.wav"))
    monkeypatch.setattr(
        processor,
        "_ensure_asr",
        lambda _episode, _wav, _force: {
            "segments": [{"words": [{"start": 0, "end": 1, "word": "hello"}]}]
        },
    )
    processor._diarizer = type(
        "D",
        (),
        {
            "backend": "senko",
            "run_with_embeddings": lambda self, _wav: (
                [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}],
                {"SPEAKER_00": [0.1, 0.2]},
            )
        },
    )()

    processor.process(store, store.episode_by_id(1))
    [sample] = store.speaker_embeddings(store.episode_by_id(1).guid)
    assert sample.embedding == (0.1, 0.2)
    assert cfg.raw_path(store.episode_by_id(1).guid, "embed").exists()
    store.close()
