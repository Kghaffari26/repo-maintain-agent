"""Untriaged issue detection (SPEC_REPO_MAINT.md §5.1)."""

from __future__ import annotations

from typing import Any

from agents.repo_maint.config import RepoConfig

MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

#: HTML comment marker the agent's own triage comments carry (§8.3). A
#: maintainer comment carrying this marker doesn't count as maintainer
#: triage -- it's the agent's own comment, not a human's.
TRIAGE_MARKER = "agents-hub:triage"


def _is_bot(user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    login = user.get("login", "")
    return user.get("type") == "Bot" or login.endswith("[bot]")


def is_own_marker_comment(comment: dict[str, Any]) -> bool:
    return TRIAGE_MARKER in (comment.get("body") or "")


def has_maintainer_comment(comments: list[dict[str, Any]]) -> bool:
    """A maintainer (OWNER/MEMBER/COLLABORATOR) commented, excluding the
    agent's own marker comments and bot accounts."""
    for comment in comments:
        if is_own_marker_comment(comment):
            continue
        if _is_bot(comment.get("user")):
            continue
        if comment.get("author_association") in MAINTAINER_ASSOCIATIONS:
            return True
    return False


def is_untriaged(issue: dict[str, Any], comments: list[dict[str, Any]], repo: RepoConfig) -> bool:
    """An open issue (not a PR) is untriaged iff all of §5.1's conditions hold."""
    labels = {label["name"] for label in issue.get("labels", [])}
    if labels & repo.triaged_labels:
        return False
    if labels & set(repo.ignore_labels):
        return False
    return not has_maintainer_comment(comments)


def filter_untriaged(
    issues: list[dict[str, Any]],
    comments_by_number: dict[int, list[dict[str, Any]]],
    repo: RepoConfig,
) -> list[dict[str, Any]]:
    """Open, non-PR issues from ``issues`` that are untriaged, in input order."""
    result = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        comments = comments_by_number.get(issue["number"], [])
        if is_untriaged(issue, comments, repo):
            result.append(issue)
    return result
