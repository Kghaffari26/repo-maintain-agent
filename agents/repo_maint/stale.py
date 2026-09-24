"""Stale PR detection and template nudges (SPEC_REPO_MAINT.md §5.4).

Nudges are computed by state, no LLM involved, and are **shown on the site
only** -- the agent never posts them, even in apply mode (§5.4). Nothing
in this module makes a write request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agents.repo_maint.metrics import FAILING_CONCLUSIONS, parse_dt

DEFAULT_STALE_DAYS = 14

_REVIEW_STATE_PRECEDENCE = ("CHANGES_REQUESTED", "APPROVED", "COMMENTED")


def latest_review_per_reviewer(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The most recent review per reviewer login."""
    latest: dict[str, dict[str, Any]] = {}
    for review in sorted(reviews, key=lambda r: r["submitted_at"]):
        reviewer = (review.get("user") or {}).get("login")
        if reviewer:
            latest[reviewer] = review
    return latest


def review_state(reviews: list[dict[str, Any]], requested_reviewers: list[dict[str, Any]]) -> str:
    """``none | review_requested | changes_requested | approved | commented`` (§5.4)."""
    latest = latest_review_per_reviewer(reviews)
    states = {review["state"] for review in latest.values()}
    for candidate in _REVIEW_STATE_PRECEDENCE:
        if candidate in states:
            return candidate.lower()
    if requested_reviewers:
        return "review_requested"
    return "none"


def last_activity_at(pr: dict[str, Any], reviews: list[dict[str, Any]]) -> datetime:
    """The latest of the PR's ``updated_at`` (covers commits/comments) and any review."""
    candidates = [parse_dt(pr["updated_at"])]
    candidates.extend(parse_dt(review["submitted_at"]) for review in reviews)
    return max(candidates)


def is_stale(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    now: datetime,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    include_drafts: bool = False,
) -> bool:
    if pr.get("draft") and not include_drafts:
        return False
    return (now - last_activity_at(pr, reviews)) >= timedelta(days=stale_days)


def failing_check_names(check_runs: list[dict[str, Any]]) -> list[str]:
    return [
        run["name"]
        for run in check_runs
        if run.get("status") == "completed" and run.get("conclusion") in FAILING_CONCLUSIONS
    ]


@dataclass
class StalePRInfo:
    pr: dict[str, Any]
    age_days: int
    last_activity_at: datetime
    review_state: str
    ci_state: str
    failing_checks: list[str]
    requested_reviewer_logins: list[str]


def build_stale_pr_info(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    check_runs: list[dict[str, Any]],
    ci_state: str,
    now: datetime,
) -> StalePRInfo:
    requested = [r["login"] for r in pr.get("requested_reviewers", []) if r.get("login")]
    return StalePRInfo(
        pr=pr,
        age_days=(now - parse_dt(pr["created_at"])).days,
        last_activity_at=last_activity_at(pr, reviews),
        review_state=review_state(reviews, pr.get("requested_reviewers", [])),
        ci_state=ci_state,
        failing_checks=failing_check_names(check_runs),
        requested_reviewer_logins=requested,
    )


def nudge(info: StalePRInfo) -> str:
    """The template nudge text for a stale PR's state (§5.4 table)."""
    author = (info.pr.get("user") or {}).get("login", "there")

    if info.review_state == "changes_requested":
        return (
            f"Hi @{author}, just checking in on this one. Are you still planning to "
            "address the requested changes? Happy to help if anything's unclear."
        )

    if info.review_state == "approved":
        if info.ci_state == "failure":
            checks = ", ".join(info.failing_checks) or "unknown checks"
            return (
                f"This is approved but CI is failing ({checks}). "
                "Could you take a look when you get a chance?"
            )
        return "This looks ready. Maintainers, is anything blocking a merge?"

    reviewers = (
        ", ".join(f"@{login}" for login in info.requested_reviewer_logins) or "@maintainers"
    )
    return (
        f"This PR has been waiting {info.age_days} days for review. "
        f"{reviewers}, could someone take a look?"
    )
