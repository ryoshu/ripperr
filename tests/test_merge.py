from podpipe.merge import UNKNOWN, merge
from podpipe.store import Store


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


def seg(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker}


def test_word_goes_to_max_overlap_and_turns_split_on_speaker():
    words = [w(0, 1, "hello"), w(1, 2, "there"), w(2.1, 3, "hi"), w(3, 4, "you")]
    segs = [seg(0, 2.05, "A"), seg(2.05, 4, "B")]
    turns = merge(words, segs)
    assert [(t["speaker"], t["text"]) for t in turns] == [("A", "hello there"), ("B", "hi you")]


def test_orphan_word_inherits_neighbour_and_far_word_stays_unknown():
    words = [w(0, 1, "a"), w(1.3, 1.5, "b"), w(50, 51, "c")]
    turns = merge(words, [seg(0, 1, "A")])
    assert [t["speaker"] for t in turns] == ["A", UNKNOWN]
    assert turns[0]["text"] == "a b"


def test_long_pause_splits_turn():
    turns = merge([w(0, 1, "a"), w(10, 11, "b")], [seg(0, 11, "A")], max_gap=2.0)
    assert len(turns) == 2


def test_search_falls_back_on_bad_fts_syntax(tmp_path):
    store = Store(tmp_path / "t.db")
    fid = store.add_feed("http://f")
    store.add_episode(fid, "g", "T", None, "http://a")
    store.replace_turns(1, [{"speaker": "A", "start": 0, "end": 1, "text": "don't stop"}])
    assert store.search("don't")  # raw MATCH raises OperationalError
    store.close()


def test_pending_skips_errors_unless_asked(tmp_path):
    store = Store(tmp_path / "t.db")
    fid = store.add_feed("http://f")
    store.add_episode(fid, "g", "T", None, "http://a")
    store.set_status(1, "error")
    assert store.pending() == []
    assert len(store.pending(retry_errors=True)) == 1
    store.close()


def _fill_orphans_reference(words, orphan_gap):
    """The original O(n^2) implementation, kept as an oracle."""
    for i, w in enumerate(words):
        if w["speaker"] != UNKNOWN:
            continue
        prev = next((x for x in reversed(words[:i]) if x["speaker"] != UNKNOWN), None)
        nxt = next((x for x in words[i + 1:] if x["speaker"] != UNKNOWN), None)
        prev_gap = w["start"] - prev["end"] if prev else float("inf")
        next_gap = nxt["start"] - w["end"] if nxt else float("inf")
        if min(prev_gap, next_gap) > orphan_gap * 4:
            continue
        w["speaker"] = prev["speaker"] if prev_gap <= next_gap else nxt["speaker"]


def test_fill_orphans_matches_original_implementation():
    import random

    from podpipe.merge import _fill_orphans

    rng = random.Random(0)
    for _ in range(500):
        t, words = 0.0, []
        for _ in range(rng.randint(0, 30)):
            t += rng.choice([0.0, 0.1, 0.5, 1.0, 3.0])
            end = t + rng.uniform(0.1, 0.5)
            words.append(
                {"start": t, "end": end, "text": "x", "speaker": rng.choice([UNKNOWN, UNKNOWN, "A", "B"])}
            )
            t = end
        expected = [dict(x) for x in words]
        actual = [dict(x) for x in words]
        _fill_orphans_reference(expected, 0.5)
        _fill_orphans(actual, 0.5)
        assert actual == expected


def test_json3_captions_to_words():
    from podpipe.youtube import captions_reliable, json3_to_result

    data = {
        "events": [
            {"tStartMs": 1000, "segs": [{"utf8": ">>"}, {"utf8": "Hello", "tOffsetMs": 0}, {"utf8": " there", "tOffsetMs": 400}]},
            {"tStartMs": 3000, "segs": [{"utf8": "[Music]"}]},
            {"tStartMs": 4000, "segs": [{"utf8": "bye"}]},
        ]
    }
    words = json3_to_result(data)["segments"][0]["words"]
    assert [(x["word"], x["start"]) for x in words] == [("Hello", 1.0), ("there", 1.4), ("bye", 4.0)]
    assert words[0]["end"] == 1.4 and words[1]["end"] == 2.4  # capped at 1s
    assert captions_reliable({"segments": [{"words": words}]}, duration=3)
    assert not captions_reliable({"segments": [{"words": words}]}, duration=300)
    assert json3_to_result({"events": [{"tStartMs": 0, "segs": [{"utf8": "a"}, {"utf8": "b"}]}]}) is None


def test_player_names_respelled_from_roster():
    from podpipe.players import correct

    roster = ["Bhayshul Tuten", "Drake Maye", "Justin Herbert", "Joe Burrow", "Michael Penix", "Adonai Mitchell"]
    words = [
        w(0, 0.4, "Basial"), w(0.4, 0.9, "Tootin,"),
        w(1, 1.3, "and"),
        w(1.3, 1.6, "Drake"), w(1.6, 1.9, "May's"),  # possessive kept
        w(2, 2.3, "Justin"), w(2.3, 2.7, "Herbert"),  # already right: untouched
        w(3, 3.3, "Green"), w(3.3, 3.6, "Bay"),  # not a player: untouched
        w(4, 4.3, "Joe"), w(4.3, 4.6, "Brady"),  # a coach, close-ish to Burrow: untouched
        w(5, 5.2, "And"), w(5.2, 5.5, "Michael"), w(5.5, 5.8, "Penix"),  # must not become Adonai Mitchell
    ]
    out, fixes = correct(words, roster)
    assert [x["text"] for x in out] == [
        "Bhayshul Tuten,", "and", "Drake Maye's", "Justin", "Herbert", "Green", "Bay", "Joe", "Brady", "And", "Michael", "Penix",
    ]
    assert out[0]["start"] == 0 and out[0]["end"] == 0.9  # spans both words for the merge
    assert fixes == {("Basial Tootin", "Bhayshul Tuten"): 1, ("Drake May", "Drake Maye"): 1}
