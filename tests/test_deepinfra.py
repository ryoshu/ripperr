from ripperr.deepinfra import guest_hints
from ripperr.models import Episode, Turn


def episode():
    return Episode(
        id=1,
        guid="episode-guid",
        source_guid="source-guid",
        feed_id=2,
        title="On the Couch",
        summary="An episode about fantasy football.",
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


def test_deepinfra_guest_hints_uses_json_chat_completion(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": '{"guests":[{"name":"Pat Fitzmaurice","evidence":"joined by Pat Fitzmaurice"}]}'
                    }
                }]
            }

    def post(url, *, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("ripperr.deepinfra.requests.post", post)
    [hint] = guest_hints(
        episode(),
        [Turn(0, "SPEAKER_01", 0, 1, "Today we are joined by Pat Fitzmaurice.")],
        token="secret",
        model="test-model",
        base_url="https://api.deepinfra.com/v1/openai",
        host_name="Sigmund Bloom",
    )

    assert seen["url"] == "https://api.deepinfra.com/v1/openai/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["json"]["model"] == "test-model"
    assert seen["json"]["response_format"] == {"type": "json_object"}
    assert seen["timeout"] == 20
    assert (hint.name, hint.source, hint.confidence) == ("Pat Fitzmaurice", "llm", 0.70)


def test_deepinfra_guest_hints_filters_host_and_existing_hints(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": '{"guests":[{"name":"Sigmund Bloom"},{"name":"Pat Fitzmaurice"},{"name":"Pat Fitzmaurice"}]}'
                    }
                }]
            }

    monkeypatch.setattr("ripperr.deepinfra.requests.post", lambda *args, **kwargs: Response())
    hints = guest_hints(
        episode(),
        [],
        token="secret",
        model="test-model",
        base_url="https://example.com",
        host_name="Sigmund Bloom",
        existing_names={"Pat Fitzmaurice"},
    )
    assert hints == []
