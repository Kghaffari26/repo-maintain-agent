"""Tests for the evals.repo_maint fixture builders and runner (§11).

These exercise the eval machinery itself (fixture shape, and that the
free injection/changelog evals run and produce the expected result shape),
plus the live evals' scoring functions and runners against a scripted model.
"""

from __future__ import annotations

from agents.repo_maint.triage import TriageResult
from evals.repo_maint import live_evals
from evals.repo_maint.build_fixtures import build_fixtures, build_labels_proposed
from evals.repo_maint.changelog_fixtures import CHANGELOG_FIXTURES
from evals.repo_maint.injection_fixtures import INJECTION_FIXTURES
from evals.repo_maint.run_evals import run_changelog_fidelity, run_injection_resistance
from tests.repo_maint.fakes import fake_llm


def test_build_fixtures_produces_exactly_40():
    assert len(build_fixtures()) == 40


def test_build_fixtures_ids_are_unique_and_sequential():
    fixtures = build_fixtures()
    ids = [f["id"] for f in fixtures]
    assert len(set(ids)) == len(ids)
    assert ids[0] == "fx-001"
    assert ids[-1] == "fx-040"


def test_duplicate_fixtures_reference_each_other():
    fixtures = build_fixtures()
    by_title = {f["title"]: f for f in fixtures}
    a = by_title["Map doesn't render on Safari 17 (again?)"]
    b = by_title["Map fails to render in Safari version 17"]
    assert a["candidates"] == [b["id"]]
    assert b["candidates"] == [a["id"]]


def test_labels_proposed_covers_every_fixture():
    fixtures = build_fixtures()
    labels_proposed = build_labels_proposed(fixtures)
    assert set(labels_proposed["entries"]) == {f["id"] for f in fixtures}


def test_labels_proposed_marks_security_bugs_p1_or_worse():
    fixtures = build_fixtures()
    labels_proposed = build_labels_proposed(fixtures)
    security_ids = [f["id"] for f in fixtures if f["kind"] == "bug_security"]
    assert security_ids
    for fixture_id in security_ids:
        band = labels_proposed["entries"][fixture_id]["expected"]["priority_band"]
        assert band in ("p0", "p1")


def test_labels_proposed_near_miss_pair_is_not_marked_duplicate():
    fixtures = build_fixtures()
    labels_proposed = build_labels_proposed(fixtures)
    near_miss = [f for f in fixtures if f["kind"] == "near_miss"]
    assert len(near_miss) == 2
    for fixture in near_miss:
        assert labels_proposed["entries"][fixture["id"]]["expected"]["duplicate_of"] is None


def test_injection_fixtures_count_is_four():
    assert len(INJECTION_FIXTURES) == 4


def test_run_injection_resistance_passes_every_fixture():
    """The real, executed eval (§11): a worst-case fully-compliant raw model
    output run through our own code must never let the disallowed parts
    survive."""
    result = run_injection_resistance()
    assert result["status"] == "ran"
    assert result["fixtures_total"] == 4
    assert result["all_passed"] is True


def test_changelog_fixtures_count_is_three():
    assert len(CHANGELOG_FIXTURES) == 3


def test_run_changelog_fidelity_has_full_ref_coverage_on_all_fixtures():
    result = run_changelog_fidelity()
    assert result["status"] == "ran"
    assert result["ref_coverage_ok_on_all_fixtures"] is True
    assert len(result["findings"]) == 3


# -- live-eval scoring (evals/repo_maint/live_evals.py) ------------------------------------



def _result(number=1, classification="bug", priority="p2", labels=()):
    return TriageResult(
        number=number,
        classification=classification,
        priority=priority,
        confidence="high",
        suggested_labels=list(labels),
    )


def _key(**entries):
    return {
        fid: {"expected": {"classification": c, "priority_band": p, "duplicate_of": d}}
        for fid, (c, p, d) in entries.items()
    }


def test_score_classification_counts_exact_matches():
    key = _key(a=("bug", "p2", None), b=("feature", "p3", None))
    score = live_evals.score_classification({"a": _result(), "b": _result()}, key)
    assert score["accuracy"] == 0.5
    assert score["passed"] is False
    assert score["misses"] == [{"id": "b", "expected": "feature", "got": "bug"}]


def test_score_priority_within_one_and_security_rule():
    key = _key(a=("bug", "p2", None), s=("bug", "p1", None))
    results = {"a": _result(priority="p3"), "s": _result(priority="p2")}
    score = live_evals.score_priority(results, key, security_ids={"s"})
    assert score["within_one_rate"] == 1.0
    assert score["security_all_p0_p1"] is False
    assert score["passed"] is False


def test_score_label_allowlist_reports_raw_rate():
    raw = {"a": {"suggested_labels": ["bug", "made-up"]}}
    score = live_evals.score_label_allowlist(raw, {"a": _result(labels=["bug"])}, {"bug"})
    assert score["raw_model_allowlisted_rate"] == 0.5
    assert score["raw_labels_outside_allowlist"] == ["made-up"]
    assert score["passed"] is True


def test_score_duplicates_precision_over_candidate_pairs():
    fixtures = [
        {"id": "a", "number": 1, "candidates": ["b"]},
        {"id": "b", "number": 2, "candidates": ["a"]},
        {"id": "c", "number": 3, "candidates": ["a"]},
    ]
    key = _key(a=("bug", "p2", "b"), b=("bug", "p2", "a"), c=("bug", "p2", None))
    raw = {
        "a": {"duplicates": [{"number": 2, "duplicate_likely": True}]},
        "b": {"duplicates": [{"number": 1, "duplicate_likely": False}]},
        "c": {"duplicates": [{"number": 1, "duplicate_likely": True}]},
    }
    score = live_evals.score_duplicates(raw, fixtures, key)
    assert score["precision"] == 0.5  # 1 true positive, 1 false positive
    assert score["recall"] == 0.5  # b->a missed
    assert score["passed"] is False


def test_run_triage_fixtures_uses_the_production_classify_path(tmp_path):
    fixtures = [
        {
            "id": "fx-1",
            "number": 1,
            "title": "Crash",
            "body": "It crashes.",
            "author_association": "NONE",
            "kind": "bug_no_repro",
        }
    ]
    output = {
        "classification": "bug",
        "priority": "p2",
        "confidence": "high",
        "suggested_labels": ["bug", "nope"],
        "missing_info": [],
        "summary": "Crash.",
        "duplicates": [],
        "first_response": "Thanks!",
    }
    llm, client = fake_llm([output], tmp_path)
    raw, results = live_evals.run_triage_fixtures(llm, fixtures)
    assert raw["fx-1"]["suggested_labels"] == ["bug", "nope"]
    assert results["fx-1"].suggested_labels == ["bug"]
    assert len(client.calls) == 1


def test_run_live_changelog_scores_first_attempts(tmp_path):
    responses = []
    for fixture in CHANGELOG_FIXTURES:
        refs = " ".join(f"({item.ref})" for item in fixture["items"])
        responses.append(f"## [Unreleased]\n### Other\n- Everything {refs}\n")
    llm, _client = fake_llm(responses, tmp_path)
    result = live_evals.run_live_changelog(llm)
    assert result["first_attempt_rate"] == 1.0
    assert result["ref_coverage_ok_on_all_fixtures"] is True
