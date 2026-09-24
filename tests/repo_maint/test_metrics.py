"""Tests for agents.repo_maint.metrics (§5.2, §11)."""

from __future__ import annotations

from datetime import UTC, datetime

from agents.repo_maint.metrics import (
    activity_12w,
    ci_state_from_check_runs,
    first_response_hours,
    median_first_response_hours,
    untriaged_over_7d,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def issue(number, created_at, author="reporter"):
    return {"number": number, "created_at": created_at, "user": {"login": author}}


def comment_at(when, login="someone", bot=False):
    return {"created_at": when, "user": {"login": login, "type": "Bot" if bot else "User"}}


# -- first response -----------------------------------------------------------


def test_first_response_hours_basic():
    an_issue = issue(1, "2026-09-01T00:00:00Z")
    comments = [comment_at("2026-09-02T12:00:00Z")]
    assert first_response_hours(an_issue, comments) == 36.0


def test_first_response_ignores_authors_own_comments():
    an_issue = issue(1, "2026-09-01T00:00:00Z", author="reporter")
    comments = [
        comment_at("2026-09-01T01:00:00Z", login="reporter"),
        comment_at("2026-09-01T05:00:00Z", login="someone-else"),
    ]
    assert first_response_hours(an_issue, comments) == 5.0


def test_first_response_ignores_bots():
    an_issue = issue(1, "2026-09-01T00:00:00Z")
    comments = [
        comment_at("2026-09-01T01:00:00Z", login="ci[bot]", bot=True),
        comment_at("2026-09-01T04:00:00Z", login="human"),
    ]
    assert first_response_hours(an_issue, comments) == 4.0


def test_first_response_none_when_no_response():
    an_issue = issue(1, "2026-09-01T00:00:00Z")
    assert first_response_hours(an_issue, []) is None


def test_median_first_response_excludes_old_issues_and_counts_no_response():
    issues_with_comments = [
        (issue(1, "2026-09-20T00:00:00Z"), [comment_at("2026-09-20T10:00:00Z")]),  # 10h, in window
        (issue(2, "2026-09-22T00:00:00Z"), [comment_at("2026-09-22T20:00:00Z")]),  # 20h, in window
        (issue(3, "2026-09-23T00:00:00Z"), []),  # no response, in window
        (issue(4, "2025-01-01T00:00:00Z"), [comment_at("2025-01-01T02:00:00Z")]),  # too old
    ]
    median, no_response_count = median_first_response_hours(issues_with_comments, NOW)
    assert median == 15.0
    assert no_response_count == 1


def test_median_first_response_all_none_when_nobody_responded():
    issues_with_comments = [(issue(1, "2026-09-20T00:00:00Z"), [])]
    median, no_response_count = median_first_response_hours(issues_with_comments, NOW)
    assert median is None
    assert no_response_count == 1


# -- CI state -------------------------------------------------------------------


def test_ci_state_none_when_no_runs():
    assert ci_state_from_check_runs([]) == "none"


def test_ci_state_success_when_all_completed_and_passing():
    runs = [{"status": "completed", "conclusion": "success"}]
    assert ci_state_from_check_runs(runs) == "success"


def test_ci_state_failure_when_any_run_failed():
    runs = [
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "failure"},
    ]
    assert ci_state_from_check_runs(runs) == "failure"


def test_ci_state_pending_when_any_run_incomplete():
    runs = [{"status": "completed", "conclusion": "success"}, {"status": "in_progress"}]
    assert ci_state_from_check_runs(runs) == "pending"


# -- 12-week activity buckets ----------------------------------------------------


def test_activity_12w_buckets_by_iso_week_and_excludes_prs():
    issues = [
        issue(1, "2026-09-21T00:00:00Z"),  # this week
        {**issue(2, "2026-09-21T00:00:00Z"), "pull_request": {}},  # excluded (PR)
        {**issue(3, "2026-09-01T00:00:00Z"), "closed_at": "2026-09-21T00:00:00Z"},
    ]
    result = activity_12w(issues, NOW)
    assert len(result.weeks) == 12
    assert result.weeks[-1] == "2026-W39"
    assert result.opened[-1] == 1  # only issue 1 opened this week (PR excluded)
    assert result.closed[-1] == 1  # issue 3 closed this week


# -- untriaged over 7 days --------------------------------------------------------


def test_untriaged_over_7d_filters_by_age():
    issues = [issue(1, "2026-09-01T00:00:00Z"), issue(2, "2026-09-23T00:00:00Z")]
    result = untriaged_over_7d(issues, NOW)
    assert [i["number"] for i in result] == [1]
