"""``data/repo_maint/state.json`` read/write (SPEC_REPO_MAINT.md §4).

State is the single source of truth for ETags, the triage/changelog
caches, and the two layers of double-post protection (``commented``,
``labeled``). It's committed to the repo, not regenerated from scratch
each run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RepoState(BaseModel):
    etags: dict[str, str | None] = Field(default_factory=dict)
    triage_cache: dict[str, dict[str, Any]] = Field(default_factory=dict)
    changelog_cache: dict[str, Any] | None = None
    commented: list[int] = Field(default_factory=list)
    labeled: dict[str, list[str]] = Field(default_factory=dict)


class State(BaseModel):
    repos: dict[str, RepoState] = Field(default_factory=dict)


def load_state(path: str | Path) -> State:
    """An empty ``State`` if the file doesn't exist yet (first run)."""
    file_path = Path(path)
    if not file_path.exists():
        return State()
    return State.model_validate(json.loads(file_path.read_text()))


def save_state(path: str | Path, state: State) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")


def get_repo_state(state: State, full_name: str) -> RepoState:
    """Get-or-create a repo's state entry, mutating ``state.repos`` in place."""
    if full_name not in state.repos:
        state.repos[full_name] = RepoState()
    return state.repos[full_name]
