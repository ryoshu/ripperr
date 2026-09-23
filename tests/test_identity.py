from ripperr.identity import guest_hints
from ripperr.models import Episode, Turn


def episode(title="On The Couch with Sigmund Bloom and Pat Fitzmaurice", summary=None):
    return Episode(
        id=1,
        guid="episode-guid",
        source_guid="source-guid",
        feed_id=2,
        title=title,
        summary=summary,
        published=None,
        audio_url="https://example.com/audio.mp3",
        source_url=None,
        audio_path=None,
        duration=None,
        status="done",
        error=None,
        updated_at="2026-09-22T00:00:00+00:00",
        revision=1,
        merged_at=None,
    )


def test_guest_hints_keep_evidence_and_filter_known_host():
    hints = guest_hints(
        episode(
            title="On the Couch with Sigmund Bloom",
            summary="Sigmund welcomes Pat Fitzmaurice to talk fantasy football.",
        ),
        [Turn(0, "SPEAKER_01", 0, 1, "Today we're joined by Sigmund Bloom and Pat Fitzmaurice.")],
        host_name="Sigmund Bloom",
    )

    assert [(hint.name, hint.source) for hint in hints] == [("Pat Fitzmaurice", "summary")]
    assert hints[0].evidence == "welcomes Pat Fitzmaurice"
    assert hints[0].confidence == 0.85


def test_guest_hints_do_not_guess_from_unstructured_text():
    assert guest_hints(episode(title="Week 2 reaction", summary="A discussion with the team."), []) == []
