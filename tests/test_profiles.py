from ripperr.api import Ripperr
from ripperr.config import Config
from ripperr.models import Turn


def test_show_profile_is_a_normalized_centroid_and_drives_matches(tmp_path):
    rip = Ripperr(Config(root=tmp_path), log=lambda _: None)
    feed = rip.add_feed("http://feed")
    rip.store.add_episode(feed.id, "one", "One", None, "http://audio/one")
    rip.store.add_episode(feed.id, "two", "Two with Guest Name", None, "http://audio/two")
    rip.store.add_episode(feed.id, "three", "Three", None, "http://audio/three")
    first, second, third = rip.episodes()
    rip.store.replace_turns(
        first.id,
        [Turn(0, "SPEAKER_01", 0, 1, "host")],
        speaker_embeddings={"SPEAKER_01": [1.0, 0.0]},
    )
    rip.set_speaker_name(first.guid, "SPEAKER_01", "Host")
    rip.store.replace_turns(
        second.id,
        [Turn(0, "SPEAKER_01", 0, 1, "host")],
        speaker_embeddings={"SPEAKER_01": [0.8, 0.6]},
    )
    rip.set_speaker_name(second.guid, "SPEAKER_01", "Host")
    rip.store.replace_turns(
        third.id,
        [Turn(0, "SPEAKER_00", 0, 1, "host")],
        speaker_embeddings={"SPEAKER_00": [0.95, 0.31]},
    )

    [profile] = rip.rebuild_speaker_profiles(feed.id)
    assert profile.name == "Host"
    assert profile.sample_count == 2
    assert abs(sum(value * value for value in profile.embedding) - 1.0) < 1e-9
    [match] = rip.speaker_matches(third.guid, min_score=0.7)
    assert (match.name, match.sample_count) == ("Host", 2)
    rip.close()
