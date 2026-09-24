"""Action planning, gating, execution, and logging (SPEC_REPO_MAINT.md §8).

Every write this module can ever issue goes through ``agents.repo_maint.gh``,
whose only two write methods are ``add_labels`` and ``add_comment`` (§8.2) --
there is no other write path to express. ``execute_actions`` additionally
requires an explicit ``gate_passed=True`` computed by
``agents.repo_maint.config.check_write_gates`` before it will call either
one; anything else is logged as ``planned``/``skipped`` with a reason,
never silently dropped (§8.1).

**Tonight's hard safety rule**: nothing in this session calls
``execute_actions`` with a real ``GitHubClient`` and ``gate_passed=True``.
Apply mode is built and tested entirely against mocks (see DECISIONS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from agents.repo_maint.config import RepoConfig
from agents.repo_maint.gh import GitHubClient
from agents.repo_maint.sanitize import sanitize_first_response
from agents.repo_maint.triage import TriageResult
from agents.repo_maint.untriaged import TRIAGE_MARKER

MAX_LABELS_PER_ISSUE = 3
SITE_NAME = "agents-hub"

ActionType = Literal["add_labels", "comment"]
ActionStatus = Literal["planned", "applied", "skipped", "failed"]


@dataclass
class Action:
    repo: str
    type: ActionType
    target: int
    detail: Any
    status: ActionStatus
    reason: str | None = None


def has_agent_marker(comments: list[dict[str, Any]]) -> bool:
    """§8.3 first layer of double-post prevention: has the agent already
    commented on this issue, ever?"""
    return any(TRIAGE_MARKER in (comment.get("body") or "") for comment in comments)


def build_comment_body(triage: TriageResult, owner: str, repo: str) -> str:
    """The §8.3 comment template, with ``first_response`` sanitized (§8.4).

    If sanitization rejects the text outright, only the fixed template
    part is posted -- never a partially-cleaned version of rejected text.
    """
    sanitize_result = sanitize_first_response(triage.first_response or "", owner, repo)
    first_response_text = "" if sanitize_result.rejected else (sanitize_result.text or "")

    labels_str = ", ".join(f"`{label}`" for label in triage.suggested_labels) or "none suggested"
    missing_str = ", ".join(triage.missing_info) or "nothing specific"

    lines = [
        f"<!-- {TRIAGE_MARKER} v1 -->",
        "\U0001f44b Thanks for opening this! An automated triage pass suggests:",
        "",
        f"- **Type:** {triage.classification.capitalize()} · "
        f"**Priority:** {triage.priority.upper()}",
        f"- **Suggested labels:** {labels_str}",
        f"- **To help us reproduce:** {missing_str}",
        "",
    ]
    if first_response_text:
        lines.append(first_response_text)
        lines.append("")
    lines.append(
        f"<sub>Automated by [{SITE_NAME}](https://github.com/{owner}/{repo}). "
        "A maintainer will follow up. Suggestions may be wrong.</sub>"
    )
    return "\n".join(lines)


def plan_actions_for_issue(
    triage: TriageResult,
    repo: RepoConfig,
    existing_comments: list[dict[str, Any]],
    already_commented: set[int],
    existing_labels: set[str],
    issue_labels: set[str] = frozenset(),
) -> list[Action]:
    """§8.2: at most one ``add_labels`` (only labels that exist, capped at 3,
    excluding any the issue already carries -- idempotent across runs) and
    at most one ``comment`` (only on issues with ``confidence != low``, and
    only once ever per issue -- both layers of the §8.3 guard)."""
    actions: list[Action] = []

    labels = [
        label
        for label in triage.suggested_labels
        if label in existing_labels and label not in issue_labels
    ]
    labels = labels[:MAX_LABELS_PER_ISSUE]
    if labels:
        actions.append(
            Action(
                repo=repo.full_name,
                type="add_labels",
                target=triage.number,
                detail=labels,
                status="planned",
            )
        )

    already_posted = has_agent_marker(existing_comments) or triage.number in already_commented
    if triage.confidence != "low" and not already_posted:
        actions.append(
            Action(
                repo=repo.full_name,
                type="comment",
                target=triage.number,
                detail="triage comment",
                status="planned",
            )
        )
    return actions


@dataclass
class RunBudget:
    """§8.2 additional limits: a cap across all repos in a run, and a
    per-repo daily cap (state.json tracks the latter across runs)."""

    max_writes_per_run: int
    max_writes_per_repo_per_day: int
    writes_today_by_repo: dict[str, int] = field(default_factory=dict)
    writes_this_run: int = 0

    def can_write(self, repo_full_name: str) -> bool:
        if self.writes_this_run >= self.max_writes_per_run:
            return False
        return self.writes_today_by_repo.get(repo_full_name, 0) < self.max_writes_per_repo_per_day

    def record_write(self, repo_full_name: str) -> None:
        self.writes_this_run += 1
        self.writes_today_by_repo[repo_full_name] = (
            self.writes_today_by_repo.get(repo_full_name, 0) + 1
        )


def execute_actions(
    actions: list[Action],
    repo: RepoConfig,
    *,
    client: GitHubClient | None,
    gate_passed: bool,
    gate_reasons: list[str],
    budget: RunBudget,
    triage_by_number: dict[int, TriageResult],
) -> list[Action]:
    """Gate -> execute (capped) -> log (§8, module layout comment on actions.py).

    Never issues a write unless ``gate_passed`` is True AND a client was
    injected AND the budget allows it -- every other case logs a reason
    instead of silently doing nothing.
    """
    owner, repo_name = repo.full_name.split("/", 1)
    results: list[Action] = []

    for action in actions:
        if not gate_passed:
            reason = "; ".join(gate_reasons) or "report mode"
            results.append(replace(action, status="planned", reason=reason))
            continue
        if client is None:
            results.append(replace(action, status="planned", reason="no write-capable client"))
            continue
        if not budget.can_write(repo.full_name):
            results.append(replace(action, status="skipped", reason="write cap reached"))
            continue

        try:
            if action.type == "add_labels":
                client.add_labels(owner, repo_name, action.target, action.detail)
            else:
                triage = triage_by_number[action.target]
                body = build_comment_body(triage, owner, repo_name)
                client.add_comment(owner, repo_name, action.target, body)
        except Exception as exc:  # noqa: BLE001 -- logged as a failed action, not raised
            results.append(replace(action, status="failed", reason=str(exc)))
            continue

        budget.record_write(repo.full_name)
        results.append(replace(action, status="applied"))

    return results
