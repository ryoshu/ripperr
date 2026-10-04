from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from ripperr.api import LeaseError, Ripperr
from ripperr.config import Config
from ripperr.store import STATUS_DOWNLOADED

from test_server import _post, _request, _server

TOKEN = {"Authorization": "Bearer secret"}


def _downloaded(rip: Ripperr, tmp_path: Path, source_guid: str, published: str) -> str:
    """An episode whose audio and 16 kHz WAV are already on disk."""
    fid = rip.add_feed("http://feed").id
    rip.store.add_episode(fid, source_guid, source_guid, published, f"http://a/{source_guid}")
    ep = next(e for e in rip.episodes() if e.source_guid == source_guid)
    src = tmp_path / f"{source_guid}.mp3"
    src.write_bytes(b"mp3")
    rip.cfg.audio_dir.mkdir(parents=True, exist_ok=True)
    (rip.cfg.audio_dir / f"{source_guid}.16k.wav").write_bytes(b"RIFFwav")
    rip.store.set_status(ep.id, STATUS_DOWNLOADED, audio_path=str(src), duration=4.0)
    return ep.guid


def _result(lease_id: str, *, space: str = "senko-campplus", model: str = "turbo") -> dict:
    return {
        "schema": 1,
        "lease_id": lease_id,
        "asr": {
            "config": {"backend": "mlx", "runtime": "9.9", "model": model, "device": "auto", "language": None},
            "segments": [{"start": 0.0, "end": 2.0, "text": "hello there", "words": [
                {"start": 0.0, "end": 0.5, "word": "hello"}, {"start": 0.6, "end": 1.0, "word": "there"},
            ]}],
        },
        "diarization": {
            "config": {"backend": "senko", "runtime": "1bcb090", "model": None, "device": "auto"},
            "segments": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"space": space, "speakers": {"SPEAKER_00": [0.1, 0.2, 0.3]}},
        },
    }


def test_claim_leases_newest_once_and_expired_leases_move_on(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    old = _downloaded(rip, tmp_path, "old", "2026-01-01T00:00:00+00:00")
    new = _downloaded(rip, tmp_path, "new", "2026-02-01T00:00:00+00:00")

    ep, lease, _ = rip.store.claim("m5", 3600)
    assert ep.guid == new
    assert rip.store.claim("m5", 3600)[0].guid == old
    assert rip.store.claim("m5", 3600) is None
    assert rip.store.pending() == []  # leased episodes are not processed in-process either

    rip2 = Ripperr(Config(root=tmp_path / "other"), log=lambda _: None)
    guid = _downloaded(rip2, tmp_path, "solo", "2026-03-01T00:00:00+00:00")
    _, stale, _ = rip2.store.claim("asleep", 0)  # expires at once
    _, fresh, _ = rip2.store.claim("awake", 3600)
    assert rip2.store.lease_holder(guid, stale) is None
    assert rip2.store.lease_holder(guid, fresh).guid == guid


def test_episode_is_claimable_only_once_its_wav_exists(tmp_path, monkeypatch):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    fid = rip.add_feed("http://feed").id
    rip.store.add_episode(fid, "ep", "Ep", None, "http://a/ep")
    claims_during_conversion = []

    def download(url, dest, title):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "ep.mp3").write_bytes(b"mp3")
        return dest / "ep.mp3"

    def to_wav16k(src, dest):
        claims_during_conversion.append(rip.claim_work("eager"))
        return dest / "ep.16k.wav"

    monkeypatch.setattr("ripperr.pipeline.feeds.download", download)
    monkeypatch.setattr("ripperr.pipeline.audio.duration_seconds", lambda src: 1.0)
    monkeypatch.setattr("ripperr.pipeline.audio.to_wav16k", to_wav16k)
    assert len(rip.prepare()) == 1
    assert claims_during_conversion == [None]
    assert rip.claim_work("m5") is not None


def test_submit_merges_once_records_model_keys_and_remerges_without_models(tmp_path):
    rip = Ripperr(Config(root=tmp_path, keep_audio=True), log=lambda _: None)
    guid = _downloaded(rip, tmp_path, "ep", "2026-01-01T00:00:00+00:00")
    _, lease, _ = rip.claim_work("m5")

    with pytest.raises(ValueError):  # a malformed body does not use up the lease
        rip.submit_work(guid, {**_result(lease), "diarization": {"segments": "nope"}})
    ep = rip.submit_work(guid, _result(lease))
    assert (ep.status, ep.revision) == ("done", 1)
    assert [t.text for t in rip.transcript(guid).turns] == ["hello there"]
    assert [e.speaker for e in rip.store.speaker_embeddings(guid)] == ["SPEAKER_00"]
    with pytest.raises(LeaseError):
        rip.submit_work(guid, _result(lease))  # retried after success: applied at most once

    # This machine has none of the worker's models, so its own cache keys differ;
    # remerge and prune_cache must follow the keys recorded with the result.
    asr_key, diar_key = rip.store.raw_keys(guid)
    assert asr_key and diar_key and asr_key != rip.cfg.cache_key("asr")
    assert rip.prune_cache() == 0
    assert rip.remerge(guid, glossary=[]).revision == 2


def test_embeddings_from_another_space_are_dropped_but_transcript_kept(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    guid = _downloaded(rip, tmp_path, "ep", "2026-01-01T00:00:00+00:00")
    _, lease, _ = rip.claim_work("linux")
    ep = rip.submit_work(guid, _result(lease, space="pyannote-wespeaker"))
    assert ep.status == "done"
    assert rip.store.speaker_embeddings(guid) == []


def test_fail_releases_lease_and_marks_error(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    guid = _downloaded(rip, tmp_path, "ep", "2026-01-01T00:00:00+00:00")
    _, lease, _ = rip.claim_work("m5")
    rip.fail_work(guid, lease, "out of memory")
    ep = rip.episode(guid)
    assert ep.status == "error" and "out of memory" in ep.error
    assert rip.claim_work("m5") is None
    assert [e.guid for e in rip.prepare(retry_errors=True)] == [guid]
    assert rip.claim_work("m5")[0].guid == guid


def test_http_worker_round_trip(tmp_path):
    cfg = Config(root=tmp_path, keep_audio=True)
    rip = Ripperr(cfg, log=lambda _: None)
    guid = _downloaded(rip, tmp_path, "ep", "2026-01-01T00:00:00+00:00")
    rip.close()

    open_server, thread = _server(cfg.db_path)  # no token configured
    try:
        status, _, _ = _post(open_server, "/v1/work/claim", {"worker": "m5", "schema": 1})
        assert status == 503  # refused: no token configured
    finally:
        open_server.shutdown()
        thread.join()

    server, thread = _server(cfg.db_path, "secret")
    try:
        status, _, body = _post(server, "/v1/work/claim", {"worker": "m5", "schema": 2}, TOKEN)
        assert status == 400 and body["supported"] == [1]
        status, _, claim = _post(server, "/v1/work/claim", {"worker": "m5", "schema": 1}, TOKEN)
        assert status == 200 and claim["guid"] == guid

        base = f"http://127.0.0.1:{server.server_port}/v1/work/{guid}"
        with urlopen(Request(f"{base}/audio?lease_id={claim['lease_id']}", headers=TOKEN)) as response:
            assert response.headers["Content-Type"] == "audio/wav"
            assert response.read() == b"RIFFwav"

        status, _, body = _request(server, "PUT", f"/v1/work/{guid}/result", _result(claim["lease_id"]), TOKEN)
        assert (status, body) == (200, {"guid": guid, "revision": 1})
        status, _, _ = _request(server, "PUT", f"/v1/work/{guid}/result", _result(claim["lease_id"]), TOKEN)
        assert status == 409

        status, _, _ = _post(server, "/v1/work/claim", {"worker": "m5", "schema": 1}, TOKEN)
        assert status == 204
    finally:
        server.shutdown()
        thread.join()
