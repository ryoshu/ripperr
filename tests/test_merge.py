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
