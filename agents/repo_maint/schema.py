"""Output schema: the site contract (SPEC_REPO_MAINT.md §6).

The shared blocks here (``Model``, ``Timestamp``, ``RunMeta``, ``KeyStat``,
``Source``, ``ModelUsage``/``TierUsage``) are **mirrors** of
``agents_core.schema``, field-for-field, copied from a real read of that
module in the agents-core repo (see DECISIONS.md/STATUS.md -- it isn't
installable yet). They exist so this agent's output validates against the
same shape the site expects *today*; once agents_core is wired in, these
mirrors should be deleted and replaced with the real import, changing
nothing about ``RepoMaintOutput`` itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, PlainSerializer

RunStatus = Literal["ok", "stale", "failed"]
GoodDirection = Literal["up", "down", "neutral"]
StatFormat = Literal[
    "currency_compact",
    "currency",
    "percent",
    "percent_signed",
    "pp_signed",
    "count",
    "count_signed",
    "count_signed_thousands",
    "decimal1",
    "days",
    "ratio",
]


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


Timestamp = Annotated[AwareDatetime, PlainSerializer(iso_z, return_type=str, when_used="json")]


class Model(BaseModel):
    """Base for published models: unknown fields are a validation error."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Source(Model):
    name: str
    url: HttpUrl
    retrieved_at: Timestamp


class TierUsage(Model):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelUsage(Model):
    fast: TierUsage = Field(default_factory=TierUsage)
    smart: TierUsage = Field(default_factory=TierUsage)


class RunMeta(Model):
    """The shared `meta` block (mirrors agents_core.schema.RunMeta)."""

    agent: str
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    run_id: str
    started_at: Timestamp
    finished_at: Timestamp
    status: RunStatus
    data_changed: bool
    cost_usd: float = Field(ge=0)
    model_usage: ModelUsage
    sources: list[Source]


class RepoMaintMeta(RunMeta):
    """The repo_maint-specific extension of the shared meta block (§6)."""

    github_requests: int = Field(ge=0)
    github_304s: int = Field(ge=0)


class KeyStat(Model):
    label: str
    value: float | None
    format: StatFormat
    delta: float | None = None
    delta_format: StatFormat | None = None
    good_direction: GoodDirection = "neutral"


# -- §6 repo_maint-specific shapes ------------------------------------------------

Role = Literal["own", "sandbox", "public_demo"]
Mode = Literal["report", "apply"]
CIState = Literal["success", "failure", "pending", "none"]
ReviewState = Literal["none", "review_requested", "changes_requested", "approved", "commented"]
ActionType = Literal["add_labels", "comment"]
ActionStatus = Literal["planned", "applied", "skipped", "failed"]
NarrativeSource = Literal["llm", "deterministic"]
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


class RepoMaintOutput(Model):
    """The full `latest.json` contract for this agent (§6)."""

    meta: RepoMaintMeta
    headline: str
    key_stats: list[KeyStat] = Field(max_length=4)
    mode: Mode
    repos: list[RepoEntry]
    actions: list[ActionEntry]
