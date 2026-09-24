"""Config loading and write-gate validation (SPEC_REPO_MAINT.md §8.1, §9).

``config/repos.toml`` is parsed into a validated ``Config``. A repo whose
``role`` is ``public_demo`` can never carry ``allow_apply = true`` -- that's
enforced here as a model validator, so a bad config fails to load at all
rather than failing later at write time.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Role = Literal["own", "sandbox", "public_demo"]
TokenName = Literal["default", "repo_maint"]

#: Roles allowed to have writes applied to them (§8.1 gate 4).
WRITABLE_ROLES: frozenset[Role] = frozenset({"own", "sandbox"})


class Settings(BaseModel):
    """``[settings]`` in repos.toml."""

    stale_days: int = 14
    include_drafts: bool = False
    dup_threshold: float = 0.45
    max_triage_per_repo_per_run: int = 25
    max_changelog_items: int = 80
    max_writes_per_run: int = 15
    max_writes_per_repo_per_day: int = 10

    model_config = {"extra": "forbid"}


class HealthPenalties(BaseModel):
    """``[health.penalties]`` in repos.toml."""

    untriaged_7d_each: int = 3
    untriaged_cap: int = 30
    stale_pr_each: int = 5
    stale_pr_cap: int = 25
    first_response_72h: int = 10
    first_response_168h: int = 15
    ci_failure: int = 20
    release_overdue: int = 10
    community_missing_each: int = 3

    model_config = {"extra": "forbid"}


class HealthConfig(BaseModel):
    """``[health]`` in repos.toml."""

    penalties: HealthPenalties = Field(default_factory=HealthPenalties)

    model_config = {"extra": "forbid"}


class RepoConfig(BaseModel):
    """One ``[[repo]]`` entry in repos.toml."""

    full_name: str
    role: Role
    allow_apply: bool = False
    token: TokenName = "default"
    label_map: dict[str, str] = Field(default_factory=dict)
    priority_labels: dict[str, str] = Field(default_factory=dict)
    extra_triaged_labels: list[str] = Field(default_factory=list)
    ignore_labels: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _public_demo_cannot_apply(self) -> RepoConfig:
        # §8.1 gate 4 / §13 acceptance criteria: this must fail to *load*,
        # not just fail the gate check at write time.
        if self.role == "public_demo" and self.allow_apply:
            raise ValueError(
                f"{self.full_name}: allow_apply must be false for role 'public_demo'"
            )
        return self

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.full_name.split("/", 1)[1]

    @property
    def triaged_labels(self) -> set[str]:
        """Labels that mark an issue as already triaged (§5.1)."""
        return (
            set(self.label_map.values())
            | set(self.priority_labels.values())
            | set(self.extra_triaged_labels)
        )


class Config(BaseModel):
    """The full parsed ``config/repos.toml``."""

    settings: Settings = Field(default_factory=Settings)
    health: HealthConfig = Field(default_factory=HealthConfig)
    repo: list[RepoConfig] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _full_names_are_unique(self) -> Config:
        names = [r.full_name for r in self.repo]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate repo entries in config: {sorted(duplicates)}")
        return self

    def get_repo(self, full_name: str) -> RepoConfig | None:
        return next((r for r in self.repo if r.full_name == full_name), None)


def load_config(path: str | Path) -> Config:
    """Parse and validate ``config/repos.toml``."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)


# -- write gates (§8.1) --------------------------------------------------


@dataclass
class GateCheck:
    """The result of checking a repo's five write gates."""

    passed: bool
    reasons: list[str] = field(default_factory=list)


def check_write_gates(
    repo: RepoConfig,
    *,
    apply_flag: bool,
    apply_changes_env: str | None,
    token_available: bool,
) -> GateCheck:
    """Check all 5 write gates from §8.1. All must pass for any write.

    1. ``--apply`` CLI flag present.
    2. ``APPLY_CHANGES`` env var (workflow-set repo variable) equals "true".
    3. The repo's ``allow_apply`` is true.
    4. The repo's role is ``own`` or ``sandbox``.
    5. A write-capable token is available for the repo.
    """
    reasons: list[str] = []
    if not apply_flag:
        reasons.append("--apply flag not set")
    if apply_changes_env != "true":
        reasons.append("APPLY_CHANGES env var is not 'true'")
    if not repo.allow_apply:
        reasons.append(f"{repo.full_name}: allow_apply is false")
    if repo.role not in WRITABLE_ROLES:
        reasons.append(f"{repo.full_name}: role '{repo.role}' cannot be written to")
    if not token_available:
        reasons.append(f"{repo.full_name}: no write-capable token available")
    return GateCheck(passed=not reasons, reasons=reasons)
