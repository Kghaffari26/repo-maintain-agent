"""The repo_maint agent, registered with agents-core under the ``agents_core.agents``
entry point as ``repo_maint`` (see pyproject.toml), so that

    uv run agents-run repo_maint              # report mode
    uv run agents-run repo_maint --dry-run    # fetch + compute; no LLM, no writes, no publish
    uv run agents-run repo_maint --apply      # gate 1 of the five in §8.1

run fetch -> transform -> analyze -> validate -> publish through ``agents_core.runner``.
Publishing (latest.json, history/, manifest-entry.json, costs-summary.json,
schema.json under ``public-data/``) is entirely the runner's job.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents_core.agent import Agent, AgentResult, RunContext
from agents_core.llm import tier_config
from agents_core.schema import Source

from agents.repo_maint import changelog as changelog_mod
from agents.repo_maint import pipeline, schema
from agents.repo_maint import triage as triage_mod
from agents.repo_maint.config import Config, RepoConfig, load_config
from agents.repo_maint.gh import GitHubClient, resolve_token
from agents.repo_maint.state import State, get_repo_state, load_state, save_state

log = logging.getLogger(__name__)

CONFIG_PATH = Path("config/repos.toml")
STATE_PATH = Path("data/repo_maint/state.json")

#: `--repos a/b,c/d` (forwarded by agents-run as an extra arg) narrows a run, §10.
REPOS_FLAG = "--repos"


def repos_filter(extra_args: list[str]) -> set[str] | None:
    """Parse ``--repos a/b,c/d`` or ``--repos=a/b,c/d`` from ``ctx.extra_args``."""
    for i, arg in enumerate(extra_args):
        value = None
        if arg == REPOS_FLAG and i + 1 < len(extra_args):
            value = extra_args[i + 1]
        elif arg.startswith(REPOS_FLAG + "="):
            value = arg.split("=", 1)[1]
        if value is not None:
            names = {v.strip() for v in value.split(",") if v.strip()}
            return names or None
    return None


@dataclass
class Fetched:
    config: Config
    state: State
    now: datetime
    repos: list[pipeline.RepoFetch]
    failed: dict[str, str]


@dataclass
class Computed:
    fetched: Fetched
    works: list[pipeline.RepoWork]


def _fingerprint(body: dict[str, Any]) -> str:
    """The published data minus what changes on every run regardless (timestamps,
    request counts, cache flags), for ``AgentResult.data_changed``."""

    def strip(x: Any) -> Any:
        if isinstance(x, dict):
            return {
                k: strip(v)
                for k, v in x.items()
                if k not in {"meta", "generated_at", "cached", schema.META_EXTRA_KEY}
            }
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x

    return json.dumps(strip(body), sort_keys=True, default=str)


class RepoMaintAgent(Agent):
    id = "repo_maint"
    name = "Repo Maintenance Agent"
    route = "/repos"
    schema_version = "1.0.0"
    expected_interval_hours = 24
    next_run_hint = "Daily 07:00 PT"
    history_keep = 90
    output_model = schema.RepoMaintOutput

    config_path: Path = CONFIG_PATH
    state_path: Path = STATE_PATH

    # -- fetch: every GitHub read, no LLM ------------------------------------------------

    def fetch(self, ctx: RunContext) -> Fetched:
        config = load_config(self.config_path)
        wanted = repos_filter(ctx.extra_args)
        if wanted is not None:
            unknown = wanted - {r.full_name for r in config.repo}
            if unknown:
                raise ValueError(
                    f"--repos names repos not in {self.config_path}: {sorted(unknown)}"
                )
            config = config.model_copy(
                update={"repo": [r for r in config.repo if r.full_name in wanted]}
            )
        state = load_state(self.state_path)
        now = ctx.started_at

        def build_client(repo: RepoConfig) -> GitHubClient:
            return GitHubClient(ctx.http, resolve_token(repo.token))

        repos, failed = pipeline.fetch_all(config, state, build_client, now)
        return Fetched(config=config, state=state, now=now, repos=repos, failed=failed)

    # -- transform: every published number, no LLM -----------------------------------------

    def transform(self, ctx: RunContext, raw: Fetched) -> Computed:
        works = [
            pipeline.compute_repo(
                f, get_repo_state(raw.state, f.repo.full_name), raw.now, raw.config
            )
            for f in raw.repos
        ]
        return Computed(fetched=raw, works=works)

    def summarize_dry_run(self, data: Computed) -> str:
        lines = [f"{len(data.works)} repo(s) fetched, {len(data.fetched.failed)} failed"]
        for name, reason in data.fetched.failed.items():
            lines.append(f"  {name}: FAILED ({reason})")
        for work in data.works:
            snap = work.fetched.snapshot
            lines.append(
                f"  {work.repo.full_name}: health {work.health.score} ({work.health.grade}), "
                f"{len(snap.open_issues)} open issues, {len(work.untriaged_issues)} untriaged "
                f"({len(work.cached_triage)} cached, {work.needs_fresh_triage} need the LLM), "
                f"{len(work.stale_items)} stale PRs, {len(work.changelog_items)} changelog items"
            )
            repo_state = get_repo_state(data.fetched.state, work.repo.full_name)
            for action in pipeline.planned_actions(work, repo_state):
                lines.append(
                    f"    planned {action.type} on #{action.target}: {action.detail} "
                    "(dry run: nothing written)"
                )
        return "\n".join(lines)

    # -- analyze: LLM triage + changelog, then actions -------------------------------------

    def analyze(self, ctx: RunContext, data: Computed) -> AgentResult:
        fetched = data.fetched

        def classify_factory(
            repo: RepoConfig, description: str | None, labels: set[str]
        ) -> triage_mod.ClassifyFn:
            return triage_mod.make_classify_fn(ctx.llm, repo, description, labels)

        result = pipeline.finish_all(
            data.works,
            fetched.config,
            fetched.state,
            fetched.now,
            failed=fetched.failed,
            apply_flag=ctx.apply,
            apply_changes_env=os.environ.get("APPLY_CHANGES"),
            classify_factory=classify_factory,
            draft_fn=changelog_mod.make_draft_fn(ctx.llm),
            draft_model=tier_config("smart").model,
        )
        save_state(self.state_path, fetched.state)

        previous = ctx.previous_latest()
        data_changed = previous is None or _fingerprint(previous) != _fingerprint(result.body)
        retrieved_at = datetime.now(UTC)
        sources = [
            Source(name=f"GitHub: {e.full_name}", url=str(e.url), retrieved_at=retrieved_at)
            for e in result.repo_entries
        ]
        return AgentResult(
            body=result.body,
            sources=sources,
            headline=result.body["headline"],
            key_stats=[schema.KeyStat.model_validate(k) for k in result.body["key_stats"]],
            data_changed=data_changed,
            items_count=len(result.repo_entries),
        )


AGENT = RepoMaintAgent()
