from ripperr.glossary import correct, load


def test_load_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "glossary.txt"
    path.write_text("# players\n\nBhayshul Tuten\n Drake Maye \n")

    assert load(path) == ["Bhayshul Tuten", "Drake Maye"]


def test_correct_preserves_timing_and_reports_name_swap():
    words = [
        {"start": 0, "end": 0.4, "text": "Basial"},
        {"start": 0.4, "end": 0.9, "text": "Tootin"},
        {"start": 1, "end": 1.5, "text": "runs"},
    ]

    corrected, fixes = correct(words, ["Bhayshul Tuten"])

    assert corrected == [
        {"start": 0, "end": 0.9, "text": "Bhayshul Tuten"},
        {"start": 1, "end": 1.5, "text": "runs"},
    ]
    assert fixes["Basial Tootin", "Bhayshul Tuten"] == 1
