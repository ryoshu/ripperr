from ripperr.calibration import calibrate_identity
from ripperr.models import Episode, SpeakerEmbedding, SpeakerName


def _episode(guid: str, index: int) -> Episode:
    return Episode(
        id=index,
        guid=guid,
        source_guid=guid,
        feed_id=1,
        title=guid,
        summary=None,
        published=None,
        audio_url="https://audio",
        source_url=None,
        audio_path=None,
        duration=None,
        status="done",
        error=None,
        updated_at="2026-09-22T00:00:00+00:00",
        revision=1,
        merged_at=None,
    )


def test_sigmund_calibration_uses_episode_held_out_profiles():
    episodes = [_episode(f"ep-{index}", index) for index in range(4)]
    names = {
        episode.guid: [
            SpeakerName(episode.guid, "SPEAKER_01", "Sigmund Bloom", "manual", None, "now"),
            SpeakerName(episode.guid, "SPEAKER_02", f"Guest {episode.id}", "manual", None, "now"),
        ]
        for episode in episodes
    }
    embeddings = {
        episode.guid: [
            SpeakerEmbedding(episode.guid, "SPEAKER_01", (1.0, 0.02 * episode.id), "now"),
            SpeakerEmbedding(episode.guid, "SPEAKER_02", (0.0, 1.0), "now"),
        ]
        for episode in episodes
    }

    result = calibrate_identity(episodes, names, embeddings, "Sigmund Bloom")

    assert (result.episode_count, result.sample_count) == (4, 8)
    assert (result.positive_count, result.negative_count) == (4, 4)
    assert result.leave_one_out_accuracy == 1.0
    assert result.probability(0.8) > 0.5
    assert result.probability(-0.8) < 0.5
