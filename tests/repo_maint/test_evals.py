"""Tests for the evals.repo_maint fixture builders and runner (§11).

These exercise the eval machinery itself (fixture shape, and that the
real injection/changelog evals actually run and produce the expected
result shape) -- not a live-model score, which isn't available tonight.
"""

from __future__ import annotations

from evals.repo_maint.build_fixtures import build_fixtures, build_labels_proposed
from evals.repo_maint.changelog_fixtures import CHANGELOG_FIXTURES
from evals.repo_maint.injection_fixtures import INJECTION_FIXTURES
from evals.repo_maint.run_evals import run_changelog_fidelity, run_injection_resistance


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
