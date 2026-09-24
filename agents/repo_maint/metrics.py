"""Deterministic repo metrics (SPEC_REPO_MAINT.md §5.2)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

FIRST_RESPONSE_WINDOW_DAYS = 90
ACTIVITY_WEEKS = 12

#: check-run conclusions that count as a CI failure (§5.2 ci_default_branch).
FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_bot(user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    login = user.get("login", "")
    return user.get("type") == "Bot" or login.endswith("[bot]")


# -- first response time -----------------------------------------------------


def first_response_hours(issue: dict[str, Any], comments: list[dict[str, Any]]) -> float | None:
    """Hours from creation to the first non-author, non-bot comment. None if
    nobody but the author (or a bot) has commented yet."""
    author_login = (issue.get("user") or {}).get("login")
    created_at = parse_dt(issue["created_at"])
    for comment in sorted(comments, key=lambda c: c["created_at"]):
        commenter = comment.get("user") or {}
        if commenter.get("login") == author_login:
            continue
        if _is_bot(commenter):
            continue
        responded_at = parse_dt(comment["created_at"])
        return (responded_at - created_at).total_seconds() / 3600
    return None


def median_first_response_hours(
    issues_with_comments: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    now: datetime,
) -> tuple[float | None, int]:
    """Over issues created in the last 90 days: (median hours, no_response_count).

    Issues with no response are excluded from the median but counted
    separately, per §5.2.
    """
    window_start = now - timedelta(days=FIRST_RESPONSE_WINDOW_DAYS)
    response_hours: list[float] = []
    no_response_count = 0
    for issue, comments in issues_with_comments:
        if parse_dt(issue["created_at"]) < window_start:
            continue
        hours = first_response_hours(issue, comments)
        if hours is None:
            no_response_count += 1
        else:
            response_hours.append(hours)
    median = statistics.median(response_hours) if response_hours else None
    return median, no_response_count


# -- CI state -----------------------------------------------------------------


def ci_state_from_check_runs(check_runs: list[dict[str, Any]]) -> str:
    """``success | failure | pending | none`` from check runs on a commit (§5.2)."""
    if not check_runs:
        return "none"
    if any(
        run.get("status") == "completed" and run.get("conclusion") in FAILING_CONCLUSIONS
        for run in check_runs
    ):
        return "failure"
    if any(run.get("status") != "completed" for run in check_runs):
        return "pending"
    return "success"


# -- 12-week activity buckets --------------------------------------------------


@dataclass
class Activity12w:
    weeks: list[str]
    opened: list[int]
    closed: list[int]


def _iso_week(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _week_start(dt: datetime) -> datetime:
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def activity_12w(issues: list[dict[str, Any]], now: datetime) -> Activity12w:
    """ISO-week buckets of issues opened vs closed over the last 12 weeks (§5.2).

    ``issues`` is the state=all/since=12w-ago read; PR items (those carrying
    a ``pull_request`` key) are excluded, matching the "issues" semantics
    used elsewhere in the spec.
    """
    this_week_start = _week_start(now)
    week_starts = [this_week_start - timedelta(weeks=i) for i in range(ACTIVITY_WEEKS - 1, -1, -1)]
    week_labels = [_iso_week(w) for w in week_starts]
    label_index = {label: i for i, label in enumerate(week_labels)}

    opened = [0] * ACTIVITY_WEEKS
    closed = [0] * ACTIVITY_WEEKS

    for issue in issues:
        if "pull_request" in issue:
            continue
        created_label = _iso_week(parse_dt(issue["created_at"]))
        if created_label in label_index:
            opened[label_index[created_label]] += 1
        closed_at = issue.get("closed_at")
        if closed_at:
            closed_label = _iso_week(parse_dt(closed_at))
            if closed_label in label_index:
                closed[label_index[closed_label]] += 1

    return Activity12w(weeks=week_labels, opened=opened, closed=closed)


# -- release age ----------------------------------------------------------------


def days_since(date: datetime, now: datetime) -> int:
    return (now - date).days


# -- counts ---------------------------------------------------------------------


@dataclass
class Counts:
    open_issues: int
    untriaged: int
    untriaged_over_7d: int
    open_prs: int
    stale_prs: int
    no_response_count: int


def untriaged_over_7d(
    untriaged_issues: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=7)
    return [i for i in untriaged_issues if parse_dt(i["created_at"]) < cutoff]


def utcnow() -> datetime:
    return datetime.now(UTC)
