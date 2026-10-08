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
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents_core import tracing
from agents_core.agent import Agent, AgentResult, RunContext
from agents_core.http import HostPolicy, Http
from agents_core.llm import LLM, tier_config
from agents_core.schema import Source

from agents.repo_maint import changelog as changelog_mod
from agents.repo_maint import fix_proposer as fix_mod
from agents.repo_maint import pipeline, schema
from agents.repo_maint import triage as triage_mod
from agents.repo_maint.config import Config, RepoConfig, load_config
from agents.repo_maint.gh import GitHubClient, resolve_token
from agents.repo_maint.state import State, get_repo_state, load_state, save_state

log = logging.getLogger(__name__)

CONFIG_PATH = Path("config/repos.toml")
STATE_PATH = Path("data/repo_maint/state.json")

#: A backstop on GitHub requests actually sent per UTC day (agents-core HostPolicy),
#: on top of §3's rate-limit floor. A full 6-repo run sends ~50-100.
GITHUB_HOST = "api.github.com"
GITHUB_DAILY_REQUEST_CAP = 2000
GITHUB_MAX_ATTEMPTS = 3

NO_KEY_WARNING = (
    "ANTHROPIC_API_KEY is not set: issues were left untriaged (unscored), changelogs"
    " use the template grouping, and no fixes were proposed"
)
REJECTED_KEY_WARNING = (
    "The Anthropic API rejected ANTHROPIC_API_KEY (HTTP {status}): issues were left"
    " untriaged (unscored), changelogs use the template grouping, and no fixes were"
    " proposed. Update the secret."
)

#: `--repos a/b,c/d` (forwarded by agents-run as an extra arg) narrows a run, §10.
REPOS_FLAG = "--repos"
#: `--approve-fix <id>[,<id>]`: a human approves fix proposals by id (§6.1).
APPROVE_FIX_FLAG = "--approve-fix"
_PROPOSAL_ID = re.compile(r"^[0-9a-f]{12}$")


def _flag_values(extra_args: list[str], flag: str) -> set[str] | None:
    """Comma-separated values of ``flag v`` or ``flag=v`` in ``ctx.extra_args``."""
    for i, arg in enumerate(extra_args):
        value = None
        if arg == flag and i + 1 < len(extra_args):
            value = extra_args[i + 1]
        elif arg.startswith(flag + "="):
            value = arg.split("=", 1)[1]
        if value is not None:
            names = {v.strip() for v in value.split(",") if v.strip()}
            return names or None
    return None


def repos_filter(extra_args: list[str]) -> set[str] | None:
    """Parse ``--repos a/b,c/d`` or ``--repos=a/b,c/d`` from ``ctx.extra_args``."""
    return _flag_values(extra_args, REPOS_FLAG)


def approved_fix_ids(extra_args: list[str]) -> set[str]:
    """Parse ``--approve-fix id1,id2``; ids are the 12-hex-digit proposal ids."""
    ids = _flag_values(extra_args, APPROVE_FIX_FLAG) or set()
    bad = sorted(i for i in ids if not _PROPOSAL_ID.match(i))
    if bad:
        raise ValueError(f"{APPROVE_FIX_FLAG}: not proposal ids: {bad}")
    return ids


def llm_key_problem(llm: LLM) -> str | None:
    """Why the LLM can't be used this run (the warning to publish), or None if it can.

    No key: ``LLM.client`` raises RuntimeError. A key the API rejects (revoked, or a
    mistyped secret) would instead fail each call with a 401/403, which isn't an
    ``LLMError``, and crash the run; it's checked once up front with a free
    ``models.list`` request (real SDK clients only, not injected fakes). Network trouble
    or a 5xx is left to the real calls."""
    try:
        client = llm.client  # builds the SDK client, raising without a key
    except RuntimeError:
        return NO_KEY_WARNING
    if type(client).__module__.split(".")[0] != "anthropic":
        return None
    try:
        client.models.list(limit=1)
    except Exception as exc:  # noqa: BLE001 -- the SDK's error types, without importing it
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return REJECTED_KEY_WARNING.format(status=status)
    return None


def llm_available(llm: LLM) -> bool:
    """False when the run should publish template/unscored output with a warning."""
    return llm_key_problem(llm) is None


def github_cache_dir(state_path: Path, full_name: str) -> Path:
    """Where one repo's conditional-read cache (``Http.download``) lives, next to
    state.json so the workflow commits it back with the rest of ``data/``."""
    return state_path.parent / "github" / full_name.replace("/", "__")


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
                if k not in {"meta", "generated_at", "cached", "key_stats"}
            }
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x

    return json.dumps(strip(body), sort_keys=True, default=str)


class RepoMaintAgent(Agent):
    id = "repo_maint"
    name = "Repo Maintenance Agent"
    route = "/repos"
    schema_version = "1.1.0"
    expected_interval_hours = 24
    next_run_hint = "Daily 07:00 PT"
    history_keep = 90
    output_model = schema.RepoMaintOutput

    config_path: Path = CONFIG_PATH
    state_path: Path = STATE_PATH

    def configure_http(self, http: Http) -> None:
        http.set_policy(
            GITHUB_HOST,
            HostPolicy(daily_budget=GITHUB_DAILY_REQUEST_CAP, max_attempts=GITHUB_MAX_ATTEMPTS),
        )

    # -- fetch: every GitHub read, no LLM ------------------------------------------------

    def fetch(self, ctx: RunContext) -> Fetched:
        config = load_config(self.config_path)
        approved_fix_ids(ctx.extra_args)  # fail fast on a malformed approval
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
            return GitHubClient(
                ctx.http,
                resolve_token(repo.token),
                cache_dir=github_cache_dir(self.state_path, repo.full_name),
            )

        repos, failed = pipeline.fetch_all(config, state, build_client, now)
        return Fetched(config=config, state=state, now=now, repos=repos, failed=failed)

    # -- transform: every published number, no LLM -----------------------------------------

    def transform(self, ctx: RunContext, raw: Fetched) -> Computed:
        works = []
        for f in raw.repos:
            with tracing.span("custom", f"compute:{f.repo.full_name}") as sp:
                work = pipeline.compute_repo(
                    f, get_repo_state(raw.state, f.repo.full_name), raw.now, raw.config
                )
                sp.set(
                    health=work.health.score,
                    untriaged=len(work.untriaged_issues),
                    stale_prs=len(work.stale_items),
                    changelog_items=len(work.changelog_items),
                )
            works.append(work)
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
        llm_problem = llm_key_problem(ctx.llm)
        use_llm = llm_problem is None
        if llm_problem:
            ctx.warn(llm_problem)

        def classify_factory(
            repo: RepoConfig, description: str | None, labels: set[str]
        ) -> triage_mod.ClassifyFn:
            return triage_mod.make_classify_fn(ctx.llm, repo, description, labels)

        settings = fetched.config.settings
        fix_ctx = fix_mod.FixContext(
            llm=ctx.llm if use_llm else None,
            approved_ids=approved_fix_ids(ctx.extra_args),
            model=tier_config(fix_mod.FIX_TIER).model,
            proposals_left=settings.max_fix_proposals_per_run,
            max_steps=settings.fix_loop_max_steps,
            max_usd=settings.fix_loop_max_usd,
        )
        previous = ctx.previous_latest()
        result = pipeline.finish_all(
            data.works,
            fetched.config,
            fetched.state,
            fetched.now,
            failed=fetched.failed,
            apply_flag=ctx.apply,
            apply_changes_env=os.environ.get("APPLY_CHANGES"),
            classify_factory=classify_factory if use_llm else None,
            draft_fn=changelog_mod.make_draft_fn(ctx.llm) if use_llm else None,
            draft_model=tier_config("smart").model,
            previous_stats=pipeline.previous_key_stats(previous),
            fix_ctx=fix_ctx,
        )
        save_state(self.state_path, fetched.state)

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
            warnings=result.warnings,
            meta_fields=result.meta_fields,
        )


AGENT = RepoMaintAgent()
