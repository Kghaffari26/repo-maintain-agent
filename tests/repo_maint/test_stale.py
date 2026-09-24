"""Tests for agents.repo_maint.stale (§5.4, §11)."""

from __future__ import annotations

from datetime import UTC, datetime

from agents.repo_maint.stale import (
    build_stale_pr_info,
    is_stale,
    last_activity_at,
    nudge,
    review_state,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def pr(
    number=1,
    updated_at="2026-09-01T00:00:00Z",
    created_at="2026-08-20T00:00:00Z",
    draft=False,
    author="author",
    requested=None,
):
    return {
        "number": number,
        "updated_at": updated_at,
        "created_at": created_at,
        "draft": draft,
        "user": {"login": author},
        "requested_reviewers": [{"login": r} for r in (requested or [])],
    }


def review(login, state, submitted_at):
    return {"user": {"login": login}, "state": state, "submitted_at": submitted_at}


# -- staleness --------------------------------------------------------------


def test_is_stale_true_after_inactivity_window():
    assert is_stale(pr(updated_at="2026-09-01T00:00:00Z"), [], NOW, stale_days=14) is True


def test_is_stale_false_within_activity_window():
    assert is_stale(pr(updated_at="2026-09-20T00:00:00Z"), [], NOW, stale_days=14) is False


def test_is_stale_false_for_drafts_by_default():
    draft_pr = pr(updated_at="2026-08-01T00:00:00Z", draft=True)
    assert is_stale(draft_pr, [], NOW, stale_days=14) is False


def test_is_stale_true_for_drafts_when_included():
    draft_pr = pr(updated_at="2026-08-01T00:00:00Z", draft=True)
    assert is_stale(draft_pr, [], NOW, stale_days=14, include_drafts=True) is True


def test_last_activity_at_uses_latest_review_over_pr_updated_at():
    a_pr = pr(updated_at="2026-09-01T00:00:00Z")
    reviews = [review("alice", "COMMENTED", "2026-09-10T00:00:00Z")]
    assert last_activity_at(a_pr, reviews) == datetime(2026, 9, 10, tzinfo=UTC)


# -- review state -------------------------------------------------------------


def test_review_state_changes_requested_wins_precedence():
    reviews = [
        review("alice", "APPROVED", "2026-09-01T00:00:00Z"),
        review("bob", "CHANGES_REQUESTED", "2026-09-02T00:00:00Z"),
    ]
    assert review_state(reviews, []) == "changes_requested"


def test_review_state_uses_latest_review_per_reviewer():
    reviews = [
        review("alice", "CHANGES_REQUESTED", "2026-09-01T00:00:00Z"),
        review("alice", "APPROVED", "2026-09-05T00:00:00Z"),  # alice later approved
    ]
    assert review_state(reviews, []) == "approved"


def test_review_state_review_requested_when_no_reviews_but_requested():
    assert review_state([], [{"login": "alice"}]) == "review_requested"


def test_review_state_none_when_nothing():
    assert review_state([], []) == "none"


# -- nudge templates (one per state, §5.4 table) --------------------------------


def test_nudge_changes_requested():
    info = build_stale_pr_info(
        pr(author="dana"),
        [review("alice", "CHANGES_REQUESTED", "2026-09-05T00:00:00Z")],
        [],
        "success",
        NOW,
    )
    text = nudge(info)
    assert text.startswith("Hi @dana, just checking in on this one.")


def test_nudge_approved_ci_failing():
    info = build_stale_pr_info(
        pr(),
        [review("alice", "APPROVED", "2026-09-05T00:00:00Z")],
        [{"name": "build", "status": "completed", "conclusion": "failure"}],
        "failure",
        NOW,
    )
    text = nudge(info)
    assert text == (
        "This is approved but CI is failing (build). "
        "Could you take a look when you get a chance?"
    )


def test_nudge_approved_ci_passing():
    info = build_stale_pr_info(
        pr(), [review("alice", "APPROVED", "2026-09-05T00:00:00Z")], [], "success", NOW
    )
    assert nudge(info) == "This looks ready. Maintainers, is anything blocking a merge?"


def test_nudge_review_requested_with_reviewers():
    info = build_stale_pr_info(
        pr(created_at="2026-09-01T00:00:00Z", requested=["carol"]), [], [], "none", NOW
    )
    text = nudge(info)
    assert "@carol" in text
    assert "waiting" in text


def test_nudge_none_state_falls_back_to_maintainers():
    info = build_stale_pr_info(pr(created_at="2026-09-01T00:00:00Z"), [], [], "none", NOW)
    text = nudge(info)
    assert "@maintainers" in text
