"""Deterministic health score (SPEC_REPO_MAINT.md §5.6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.repo_maint.config import HealthPenalties

COMMUNITY_FILE_COUNT = 3  # README, LICENSE, CONTRIBUTING


@dataclass
class HealthBreakdownItem:
    reason: str
    points: int  # negative (or zero)


@dataclass
class HealthResult:
    score: int
    grade: str
    breakdown: list[HealthBreakdownItem] = field(default_factory=list)


def grade_for(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def missing_community_files(profile: dict[str, Any] | None) -> list[str]:
    """README/LICENSE/CONTRIBUTING missing per ``GET /repos/{o}/{r}/community/profile``.

    A ``None`` profile (the read failed or was skipped, e.g. under the rate
    limit guard) is treated as "unknown" rather than "missing everything" --
    it contributes no penalty rather than a false one.
    """
    if profile is None:
        return []
    files = profile.get("files") or {}
    missing = []
    if not files.get("readme"):
        missing.append("README")
    if not files.get("license"):
        missing.append("LICENSE")
    if not files.get("contributing"):
        missing.append("CONTRIBUTING")
    return missing


def compute_health(
    *,
    untriaged_over_7d_count: int,
    stale_pr_count: int,
    median_first_response_hours: float | None,
    ci_default_branch: str,
    merged_since_release: int,
    days_since_release: int | None,
    community_profile: dict[str, Any] | None,
    penalties: HealthPenalties,
) -> HealthResult:
    """The score starts at 100 and subtracts penalties (§5.6); each shows in the breakdown."""
    breakdown: list[HealthBreakdownItem] = []

    if untriaged_over_7d_count:
        points = min(untriaged_over_7d_count * penalties.untriaged_7d_each, penalties.untriaged_cap)
        plural = "s" if untriaged_over_7d_count != 1 else ""
        reason = f"{untriaged_over_7d_count} untriaged issue{plural} older than 7 days"
        breakdown.append(HealthBreakdownItem(reason, -points))

    if stale_pr_count:
        points = min(stale_pr_count * penalties.stale_pr_each, penalties.stale_pr_cap)
        plural = "s" if stale_pr_count != 1 else ""
        breakdown.append(HealthBreakdownItem(f"{stale_pr_count} stale PR{plural}", -points))

    if median_first_response_hours is not None and median_first_response_hours > 72:
        points = (
            penalties.first_response_168h
            if median_first_response_hours > 168
            else penalties.first_response_72h
        )
        reason = f"Median first response {median_first_response_hours:.0f}h"
        breakdown.append(HealthBreakdownItem(reason, -points))

    if ci_default_branch == "failure":
        breakdown.append(HealthBreakdownItem("CI failing on default branch", -penalties.ci_failure))

    if merged_since_release >= 10 and days_since_release is not None and days_since_release > 90:
        breakdown.append(
            HealthBreakdownItem(
                f"{merged_since_release} PRs merged since last release ({days_since_release}d ago)",
                -penalties.release_overdue,
            )
        )

    missing = missing_community_files(community_profile)
    if missing:
        cap = penalties.community_missing_each * COMMUNITY_FILE_COUNT
        points = min(len(missing) * penalties.community_missing_each, cap)
        breakdown.append(HealthBreakdownItem(f"Missing {', '.join(missing)}", -points))

    score = max(0, 100 + sum(item.points for item in breakdown))
    return HealthResult(score=score, grade=grade_for(score), breakdown=breakdown)
