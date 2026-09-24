"""Tests for agents.repo_maint.duplicates (§5.3, §11)."""

from __future__ import annotations

from agents.repo_maint.duplicates import DuplicateIndex, build_corpus, make_document


def issue(number, title, body=""):
    return {"number": number, "title": title, "body": body}


def test_make_document_strips_code_blocks_and_lowercases():
    doc = make_document(issue(1, "Map Crashes", "Steps:\n```python\nprint(1)\n```\nDone"))
    assert "```" not in doc
    assert "print(1)" not in doc
    assert doc == doc.lower()
    assert "map crashes" in doc


def test_make_document_truncates_body_to_1000_chars():
    doc = make_document(issue(1, "t", "x" * 5000))
    assert len(doc) <= len("t ") + 1000 + 1


def test_known_duplicate_pair_scores_at_or_above_threshold():
    body_1 = "The map component fails to render on Safari 17."
    body_2 = "Map component fails to render when using Safari version 17."
    body_3 = "Please add a setting to toggle dark mode in the app."
    corpus = [
        issue(1, "Map doesn't render on Safari 17", body_1),
        issue(2, "Map fails to render in Safari 17", body_2),
        issue(3, "Add dark mode toggle", body_3),
    ]
    index = DuplicateIndex(corpus)
    candidates = index.candidates(1, threshold=0.45)
    numbers = [c["number"] for c, _score in candidates]
    assert 2 in numbers
    assert 3 not in numbers


def test_unrelated_issue_scores_below_threshold():
    body_1 = "The map component fails to render on Safari 17."
    body_2 = "It would help to export the results table as CSV."
    corpus = [
        issue(1, "Map doesn't render on Safari 17", body_1),
        issue(2, "Add CSV export button", body_2),
    ]
    index = DuplicateIndex(corpus)
    candidates = index.candidates(1, threshold=0.45)
    assert candidates == []


def test_candidates_capped_at_top_k():
    corpus = [issue(1, "Login fails with 500 error")]
    corpus += [issue(n, "Login fails with 500 error too") for n in range(2, 8)]
    index = DuplicateIndex(corpus)
    candidates = index.candidates(1, threshold=0.0, top_k=3)
    assert len(candidates) == 3


def test_candidates_sorted_descending_by_similarity():
    corpus = [
        issue(1, "Crash on startup when opening a large file"),
        issue(2, "Crash on startup when opening a large file immediately"),
        issue(3, "Crash on startup"),
    ]
    index = DuplicateIndex(corpus)
    candidates = index.candidates(1, threshold=0.0)
    scores = [score for _c, score in candidates]
    assert scores == sorted(scores, reverse=True)


def test_candidates_for_unknown_issue_number_is_empty():
    index = DuplicateIndex([issue(1, "Login page broken"), issue(2, "Export button missing")])
    assert index.candidates(999) == []


def test_empty_corpus_returns_no_candidates():
    index = DuplicateIndex([])
    assert index.candidates(1) == []


def test_build_corpus_merges_open_and_closed_excludes_prs_and_dedupes():
    open_issues = [issue(1, "a"), {**issue(2, "b"), "pull_request": {}}]
    closed_issues = [issue(1, "a"), issue(3, "c")]  # 1 appears in both
    corpus = build_corpus(open_issues, closed_issues)
    numbers = sorted(i["number"] for i in corpus)
    assert numbers == [1, 3]
