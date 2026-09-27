"""Output schema: the site contract (SPEC_REPO_MAINT.md §6).

The shared blocks (``Model``, ``Timestamp``, ``RunMeta``, ``KeyStat``, ...) come
from ``agents_core.schema``. ``agents_core.runner`` builds ``meta`` itself and
validates ``{**body, "meta": meta}`` against ``RepoMaintOutput``; the §6 meta
extensions (``github_requests``, ``github_304s``) are declared on ``RepoMaintMeta``
and returned in ``AgentResult.meta_fields`` (agents-core >= v0.2.0).
"""

from __future__ import annotations

from typing import Any, Literal

from agents_core.schema import (
    AgentOutput,
    KeyStat,
    Model,
    NarrativeSource,
    RunMeta,
    Timestamp,
)
from pydantic import Field, HttpUrl

__all__ = ["KeyStat", "NarrativeSource"]


class RepoMaintMeta(RunMeta):
    """The repo_maint-specific extension of the shared meta block (§6)."""

    github_requests: int = Field(default=0, ge=0)
    github_304s: int = Field(default=0, ge=0)


# -- §6 repo_maint-specific shapes ------------------------------------------------

Role = Literal["own", "sandbox", "public_demo"]
Mode = Literal["report", "apply"]
CIState = Literal["success", "failure", "pending", "none"]
ReviewState = Literal["none", "review_requested", "changes_requested", "approved", "commented"]
ActionType = Literal["add_labels", "comment"]
ActionStatus = Literal["planned", "applied", "skipped", "failed"]
ChangelogSource = Literal["pull_requests", "commits"]


class HealthBreakdown(Model):
    reason: str
    points: int


class HealthBlock(Model):
    score: int = Field(ge=0, le=100)
    grade: Literal["A", "B", "C", "D", "F"]
    breakdown: list[HealthBreakdown] = Field(default_factory=list)


class Counts(Model):
    open_issues: int = Field(ge=0)
    untriaged: int = Field(ge=0)
    untriaged_over_7d: int = Field(ge=0)
    open_prs: int = Field(ge=0)
    stale_prs: int = Field(ge=0)
    no_response_count: int = Field(ge=0)


class Activity12w(Model):
    weeks: list[str]
    opened: list[int]
    closed: list[int]


class DuplicateRef(Model):
    number: int
    url: HttpUrl
    title: str
    similarity: float = Field(ge=0, le=1)
    state: Literal["open", "closed"]


class AppliedInfo(Model):
    labels: list[str] = Field(default_factory=list)
    commented: bool = False


class TriageItem(Model):
    number: int
    title: str
    url: HttpUrl
    author: str
    created_at: Timestamp
    classification: Literal["bug", "feature", "question", "docs", "chore", "other"]
    priority: Literal["p0", "p1", "p2", "p3"]
    confidence: Literal["low", "medium", "high"]
    suggested_labels: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    summary: str | None = None
    duplicates: list[DuplicateRef] = Field(default_factory=list)
    applied: AppliedInfo = Field(default_factory=AppliedInfo)
    cached: bool = False


class StalePRItem(Model):
    number: int
    title: str
    url: HttpUrl
    author: str
    age_days: int = Field(ge=0)
    last_activity_at: Timestamp
    review_state: ReviewState
    ci_state: CIState
    nudge: str


class ChangelogBlock(Model):
    base_ref: str | None
    base_date: str
    source: ChangelogSource
    item_count: int = Field(ge=0)
    suggested_version: str | None
    markdown: str
    narrative_source: NarrativeSource
    model: str | None = None
    generated_at: Timestamp
    cached: bool


class RepoEntry(Model):
    full_name: str
    url: HttpUrl
    role: Role
    allow_apply: bool
    partial: bool = False
    health: HealthBlock
    counts: Counts
    median_first_response_hours: float | None = None
    ci_default_branch: CIState
    days_since_release: int | None = None
    activity_12w: Activity12w
    triage: list[TriageItem] = Field(default_factory=list)
    stale_prs: list[StalePRItem] = Field(default_factory=list)
    changelog: ChangelogBlock


class ActionEntry(Model):
    repo: str
    type: ActionType
    target: int
    detail: Any
    status: ActionStatus
    reason: str | None = None


class RepoMaintOutput(AgentOutput):
    """The full `latest.json` contract for this agent (§6)."""

    meta: RepoMaintMeta
    headline: str
    key_stats: list[KeyStat] = Field(max_length=4)
    mode: Mode
    repos: list[RepoEntry]
    actions: list[ActionEntry]
