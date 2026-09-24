"""Tests for agents.repo_maint.health (§5.6, §11)."""

from __future__ import annotations

from agents.repo_maint.config import HealthPenalties
from agents.repo_maint.health import compute_health, grade_for, missing_community_files

PENALTIES = HealthPenalties()  # defaults from §9


COMPLETE_PROFILE = {
    "files": {
        "readme": {"name": "README.md"},
        "license": {"name": "LICENSE"},
        "contributing": {"name": "CONTRIBUTING.md"},
    }
}


def base_kwargs(**overrides):
    defaults = dict(
        untriaged_over_7d_count=0,
        stale_pr_count=0,
        median_first_response_hours=None,
        ci_default_branch="success",
        merged_since_release=0,
        days_since_release=None,
        community_profile=COMPLETE_PROFILE,
        penalties=PENALTIES,
    )
    defaults.update(overrides)
    return defaults


def test_perfect_repo_scores_100_grade_a():
    result = compute_health(**base_kwargs())
    assert result.score == 100
    assert result.grade == "A"
    assert result.breakdown == []


def test_untriaged_penalty_and_cap():
    result = compute_health(**base_kwargs(untriaged_over_7d_count=2))
    assert result.score == 100 - 2 * PENALTIES.untriaged_7d_each
    result_capped = compute_health(**base_kwargs(untriaged_over_7d_count=100))
    assert result_capped.score == 100 - PENALTIES.untriaged_cap


def test_stale_pr_penalty_and_cap():
    result = compute_health(**base_kwargs(stale_pr_count=1))
    assert result.score == 100 - PENALTIES.stale_pr_each
    result_capped = compute_health(**base_kwargs(stale_pr_count=100))
    assert result_capped.score == 100 - PENALTIES.stale_pr_cap


def test_first_response_over_72h_penalty():
    result = compute_health(**base_kwargs(median_first_response_hours=80))
    assert result.score == 100 - PENALTIES.first_response_72h


def test_first_response_over_168h_uses_larger_penalty():
    result = compute_health(**base_kwargs(median_first_response_hours=200))
    assert result.score == 100 - PENALTIES.first_response_168h


def test_first_response_at_or_below_72h_no_penalty():
    result = compute_health(**base_kwargs(median_first_response_hours=72))
    assert result.score == 100


def test_ci_failure_penalty():
    result = compute_health(**base_kwargs(ci_default_branch="failure"))
    assert result.score == 100 - PENALTIES.ci_failure


def test_release_overdue_penalty_requires_both_conditions():
    overdue = compute_health(**base_kwargs(merged_since_release=10, days_since_release=91))
    assert overdue.score == 100 - PENALTIES.release_overdue

    not_enough_merged = compute_health(**base_kwargs(merged_since_release=9, days_since_release=91))
    assert not_enough_merged.score == 100

    not_overdue_yet = compute_health(**base_kwargs(merged_since_release=10, days_since_release=90))
    assert not_overdue_yet.score == 100


def test_missing_community_files_each_and_cap():
    profile = {"files": {"readme": None, "license": None, "contributing": None}}
    assert missing_community_files(profile) == ["README", "LICENSE", "CONTRIBUTING"]
    result = compute_health(**base_kwargs(community_profile=profile))
    assert result.score == 100 - PENALTIES.community_missing_each * 3


def test_missing_community_files_partial():
    profile = {"files": {"readme": {"name": "README.md"}, "license": None, "contributing": None}}
    assert missing_community_files(profile) == ["LICENSE", "CONTRIBUTING"]


def test_unknown_community_profile_is_not_penalized():
    result = compute_health(**base_kwargs(community_profile=None))
    assert result.score == 100


def test_score_never_goes_below_zero():
    result = compute_health(
        **base_kwargs(
            untriaged_over_7d_count=100,
            stale_pr_count=100,
            median_first_response_hours=500,
            ci_default_branch="failure",
            merged_since_release=50,
            days_since_release=365,
            community_profile={"files": {}},
        )
    )
    assert result.score == 0
    assert result.grade == "F"


def test_grade_boundaries():
    assert grade_for(100) == "A"
    assert grade_for(90) == "A"
    assert grade_for(89) == "B"
    assert grade_for(80) == "B"
    assert grade_for(79) == "C"
    assert grade_for(70) == "C"
    assert grade_for(69) == "D"
    assert grade_for(60) == "D"
    assert grade_for(59) == "F"
    assert grade_for(0) == "F"
