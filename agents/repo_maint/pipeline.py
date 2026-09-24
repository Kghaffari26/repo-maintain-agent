"""End-to-end orchestration: fetch -> compute -> triage -> changelog -> actions
-> publish (SPEC_REPO_MAINT.md §4). This is the module the workflow and the
report-mode CLI both call into.

Like ``gh.py``, this module has no networking implementation of its own --
callers inject a per-repo HTTP client via ``build_client``. It also never
calls an LLM directly: ``classify_fn``/``draft_fn`` are injected too, and
default to ``None`` (see DECISIONS.md -- no ANTHROPIC_API_KEY tonight),
in which case triage produces no fresh results (only cache hits) and the
changelog always uses its deterministic fallback.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from agents.repo_maint import (
    actions as actions_mod,
)
from agents.repo_maint import (
    changelog as changelog_mod,
)
from agents.repo_maint import (
    duplicates as duplicates_mod,
)
from agents.repo_maint import (
    fetch as fetch_mod,
)
from agents.repo_maint import (
    health as health_mod,
)
from agents.repo_maint import (
    metrics as metrics_mod,
)
from agents.repo_maint import (
    schema,
)
from agents.repo_maint import (
    stale as stale_mod,
)
from agents.repo_maint import (
    triage as triage_mod,
)
from agents.repo_maint import (
    untriaged as untriaged_mod,
)
from agents.repo_maint.config import Config, RepoConfig, check_write_gates
from agents.repo_maint.gh import GitHubClient
from agents.repo_maint.state import RepoState, State, get_repo_state

SCHEMA_VERSION = "1.0.0"


def _issue_url(repo: RepoConfig, number: int) -> str:
    return f"https://github.com/{repo.full_name}/issues/{number}"


def _pr_url(repo: RepoConfig, number: int) -> str:
    return f"https://github.com/{repo.full_name}/pull/{number}"


def _repo_url(repo: RepoConfig) -> str:
    return f"https://github.com/{repo.full_name}"


def _build_triage_items(
    triage_results: list[tuple[triage_mod.TriageResult | None, bool]],
    repo: RepoConfig,
    issues_by_number: dict[int, dict[str, Any]],
) -> list[schema.TriageItem]:
    """Only issues with an actual result (a cache hit, tonight -- see module
    docstring) are published; the rest simply aren't in the list yet."""
    items = []
    for result, cached in triage_results:
        if result is None:
            continue
        issue = issues_by_number[result.number]
        duplicates = [
            schema.DuplicateRef(
                number=d["number"],
                url=_issue_url(repo, d["number"]),
                title=d["title"],
                similarity=d.get("similarity", 0.0),
                state=d.get("state", "open"),
            )
            for d in result.duplicates
        ]
        items.append(
            schema.TriageItem(
                number=result.number,
                title=issue["title"],
                url=_issue_url(repo, result.number),
                author=(issue.get("user") or {}).get("login", "unknown"),
                created_at=metrics_mod.parse_dt(issue["created_at"]),
                classification=result.classification,
                priority=result.priority,
                confidence=result.confidence,
                suggested_labels=result.suggested_labels,
                missing_info=result.missing_info,
                summary=result.summary,
                duplicates=duplicates,
                applied=schema.AppliedInfo(labels=[], commented=False),
                cached=cached,
            )
        )
    return items


def _build_stale_pr_items(
    open_prs: list[dict[str, Any]],
    pr_reviews: dict[int, list[dict[str, Any]]],
    pr_check_runs: dict[str, list[dict[str, Any]]],
    repo: RepoConfig,
    now: datetime,
    stale_days: int,
    include_drafts: bool,
) -> list[schema.StalePRItem]:
    items = []
    for pr in open_prs:
        reviews = pr_reviews.get(pr["number"], [])
        is_stale = stale_mod.is_stale(
            pr, reviews, now, stale_days=stale_days, include_drafts=include_drafts
        )
        if not is_stale:
            continue
        head_sha = (pr.get("head") or {}).get("sha", "")
        check_runs = pr_check_runs.get(head_sha, [])
        ci_state = metrics_mod.ci_state_from_check_runs(check_runs)
        info = stale_mod.build_stale_pr_info(pr, reviews, check_runs, ci_state, now)
        items.append(
            schema.StalePRItem(
                number=pr["number"],
                title=pr["title"],
                url=_pr_url(repo, pr["number"]),
                author=(pr.get("user") or {}).get("login", "unknown"),
                age_days=info.age_days,
                last_activity_at=info.last_activity_at,
                review_state=info.review_state,
                ci_state=info.ci_state,
                nudge=stale_mod.nudge(info),
            )
        )
    return items


def run_repo(
    repo: RepoConfig,
    client: GitHubClient,
    repo_state: RepoState,
    now: datetime,
    config: Config,
    *,
    apply_flag: bool,
    apply_changes_env: str | None,
    token_available: bool,
    budget: actions_mod.RunBudget,
    classify_fn: triage_mod.ClassifyFn | None = None,
    draft_fn: changelog_mod.DraftFn | None = None,
) -> tuple[schema.RepoEntry, list[schema.ActionEntry]]:
    settings = config.settings
    penalties = config.health.penalties

    snapshot = fetch_mod.fetch_repo(client, repo, now=now)
    default_branch = snapshot.meta.get("default_branch", "main")

    # -- untriaged + duplicates ---------------------------------------------
    untriaged_issues = untriaged_mod.filter_untriaged(
        snapshot.open_issues, snapshot.comments_by_number, repo
    )
    untriaged_over_7d = metrics_mod.untriaged_over_7d(untriaged_issues, now)

    corpus = duplicates_mod.build_corpus(snapshot.open_issues, snapshot.closed_issues_180d)
    dup_index = duplicates_mod.DuplicateIndex(corpus)
    candidates_by_number: dict[int, list[dict[str, Any]]] = {}
    for issue in untriaged_issues:
        scored = dup_index.candidates(issue["number"], threshold=settings.dup_threshold)
        candidates_by_number[issue["number"]] = [
            {
                "number": c["number"],
                "title": c["title"],
                "state": c.get("state", "open"),
                "similarity": score,
            }
            for c, score in scored
        ]

    # -- triage (cache-only tonight; see module docstring) -------------------
    triage_results = triage_mod.triage_repo(
        untriaged_issues,
        candidates_by_number,
        snapshot.labels,
        repo,
        repo_state.triage_cache,
        classify_fn,
        settings.max_triage_per_repo_per_run,
    )
    issues_by_number = {i["number"]: i for i in snapshot.open_issues}
    triage_items = _build_triage_items(triage_results, repo, issues_by_number)

    for issue, (result, _cached) in zip(untriaged_issues, triage_results, strict=False):
        if result is not None:
            repo_state.triage_cache[str(issue["number"])] = {
                "hash": triage_mod.content_hash(issue),
                "prompt_version": triage_mod.PROMPT_VERSION,
                "result": asdict(result),
            }

    # -- stale PRs ------------------------------------------------------------
    stale_items = _build_stale_pr_items(
        snapshot.open_prs,
        snapshot.pr_reviews,
        snapshot.pr_check_runs,
        repo,
        now,
        settings.stale_days,
        settings.include_drafts,
    )

    # -- metrics ----------------------------------------------------------------
    issues_with_comments = [
        (i, snapshot.comments_by_number.get(i["number"], [])) for i in snapshot.open_issues
    ]
    median_hours, no_response_count = metrics_mod.median_first_response_hours(
        issues_with_comments, now
    )
    ci_default_branch = metrics_mod.ci_state_from_check_runs(snapshot.default_branch_check_runs)
    activity = metrics_mod.activity_12w(snapshot.recent_activity, now)

    # -- changelog ----------------------------------------------------------------
    base = changelog_mod.select_base(snapshot.latest_release, snapshot.tags, now)
    merged_prs, changelog_partial = fetch_mod.fetch_merged_prs_since(client, repo, base.date)
    items = changelog_mod.select_pull_requests(
        merged_prs, base, default_branch, settings.max_changelog_items
    )
    content_source: schema.ChangelogSource = "pull_requests"
    if not items and base.ref:
        commits = fetch_mod.fetch_compare_commits(client, repo, base.ref, default_branch)
        items = changelog_mod.select_commits(commits, settings.max_changelog_items)
        content_source = "commits"

    suggested_version = changelog_mod.suggest_version(items, base)
    heading = f"## [{suggested_version}] - Unreleased" if suggested_version else "## Unreleased"
    changelog_result = changelog_mod.build_changelog(
        base=base,
        items=items,
        content_source=content_source,
        version_heading=heading,
        cache=repo_state.changelog_cache,
        draft_fn=draft_fn,
    )
    repo_state.changelog_cache = {
        "base_ref": changelog_result.base_ref,
        "pr_set_hash": changelog_mod.pr_set_hash(items),
        "markdown": changelog_result.markdown,
        "narrative_source": changelog_result.narrative_source,
    }

    # -- health -------------------------------------------------------------------
    days_since_release = None
    if base.source in ("release", "tag"):
        days_since_release = metrics_mod.days_since(base.date, now)
    health = health_mod.compute_health(
        untriaged_over_7d_count=len(untriaged_over_7d),
        stale_pr_count=len(stale_items),
        median_first_response_hours=median_hours,
        ci_default_branch=ci_default_branch,
        merged_since_release=len(merged_prs),
        days_since_release=days_since_release,
        community_profile=snapshot.community_profile,
        penalties=penalties,
    )

    # -- actions: plan -> gate -> execute -> log (§8) ------------------------------
    gate = check_write_gates(
        repo,
        apply_flag=apply_flag,
        apply_changes_env=apply_changes_env,
        token_available=token_available,
    )
    action_entries: list[schema.ActionEntry] = []
    for result, _cached in triage_results:
        if result is None:
            continue
        issue = issues_by_number[result.number]
        existing_comments = snapshot.comments_by_number.get(result.number, [])
        planned = actions_mod.plan_actions_for_issue(
            result,
            repo,
            existing_comments,
            set(repo_state.commented),
            snapshot.labels,
            issue_labels={label["name"] for label in issue.get("labels", [])},
        )
        executed = actions_mod.execute_actions(
            planned,
            repo,
            client=client if gate.passed else None,
            gate_passed=gate.passed,
            gate_reasons=gate.reasons,
            budget=budget,
            triage_by_number={result.number: result},
        )
        for action in executed:
            action_entries.append(
                schema.ActionEntry(
                    repo=action.repo,
                    type=action.type,
                    target=action.target,
                    detail=action.detail,
                    status=action.status,
                    reason=action.reason,
                )
            )
            if action.status == "applied":
                if action.type == "add_labels":
                    repo_state.labeled[str(action.target)] = list(action.detail)
                elif action.type == "comment":
                    repo_state.commented.append(action.target)

    repo_entry = schema.RepoEntry(
        full_name=repo.full_name,
        url=_repo_url(repo),
        role=repo.role,
        allow_apply=repo.allow_apply,
        partial=snapshot.partial or changelog_partial,
        health=schema.HealthBlock(
            score=health.score,
            grade=health.grade,
            breakdown=[
                schema.HealthBreakdown(reason=b.reason, points=b.points) for b in health.breakdown
            ],
        ),
        counts=schema.Counts(
            open_issues=len(snapshot.open_issues),
            untriaged=len(untriaged_issues),
            untriaged_over_7d=len(untriaged_over_7d),
            open_prs=len(snapshot.open_prs),
            stale_prs=len(stale_items),
            no_response_count=no_response_count,
        ),
        median_first_response_hours=median_hours,
        ci_default_branch=ci_default_branch,
        days_since_release=days_since_release,
        activity_12w=schema.Activity12w(
            weeks=activity.weeks, opened=activity.opened, closed=activity.closed
        ),
        triage=triage_items,
        stale_prs=stale_items,
        changelog=schema.ChangelogBlock(
            base_ref=changelog_result.base_ref,
            base_date=changelog_result.base_date,
            source=changelog_result.source,
            item_count=changelog_result.item_count,
            suggested_version=changelog_result.suggested_version,
            markdown=changelog_result.markdown,
            narrative_source=changelog_result.narrative_source,
            model=None,
            generated_at=now,
            cached=changelog_result.cached,
        ),
    )
    return repo_entry, action_entries


def run(
    config: Config,
    state: State,
    build_client: Callable[[RepoConfig], GitHubClient],
    *,
    apply_flag: bool = False,
    apply_changes_env: str | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
    classify_fn: triage_mod.ClassifyFn | None = None,
    draft_fn: changelog_mod.DraftFn | None = None,
) -> schema.RepoMaintOutput:
    """Runs every configured repo and returns the full §6 output. Mutates
    ``state`` in place (caller is responsible for saving it)."""
    now = now or datetime.now(UTC)
    run_id = run_id or str(uuid.uuid4())
    budget = actions_mod.RunBudget(
        max_writes_per_run=config.settings.max_writes_per_run,
        max_writes_per_repo_per_day=config.settings.max_writes_per_repo_per_day,
    )

    repo_entries: list[schema.RepoEntry] = []
    all_actions: list[schema.ActionEntry] = []
    total_requests = 0
    total_304s = 0

    for repo in config.repo:
        client = build_client(repo)
        repo_state = get_repo_state(state, repo.full_name)
        entry, actions_out = run_repo(
            repo,
            client,
            repo_state,
            now,
            config,
            apply_flag=apply_flag,
            apply_changes_env=apply_changes_env,
            token_available=True,  # build_client already resolved a token, or would have raised
            budget=budget,
            classify_fn=classify_fn,
            draft_fn=draft_fn,
        )
        repo_entries.append(entry)
        all_actions.extend(actions_out)
        total_requests += client.requests_made
        total_304s += client.not_modified_count

    mode: schema.Mode = "apply" if any(a.status == "applied" for a in all_actions) else "report"
    scores = [entry.health.score for entry in repo_entries]
    avg_health = round(sum(scores) / len(scores)) if scores else None
    total_untriaged = sum(entry.counts.untriaged for entry in repo_entries)
    total_stale = sum(len(entry.stale_prs) for entry in repo_entries)

    headline = (
        f"{len(repo_entries)} repo{'s' if len(repo_entries) != 1 else ''} watched: "
        f"{total_untriaged} untriaged issue{'s' if total_untriaged != 1 else ''}, "
        f"{total_stale} stale PR{'s' if total_stale != 1 else ''}."
    )

    meta = schema.RepoMaintMeta(
        agent="repo_maint",
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        started_at=now,
        finished_at=datetime.now(UTC),
        status="ok",
        data_changed=True,
        cost_usd=0.0,
        model_usage=schema.ModelUsage(),
        sources=[],
        github_requests=total_requests,
        github_304s=total_304s,
    )

    key_stats = [
        schema.KeyStat(
            label="Untriaged issues", value=total_untriaged, format="count", good_direction="down"
        ),
    ]
    if avg_health is not None:
        avg_health_stat = schema.KeyStat(
            label="Avg health", value=avg_health, format="count", good_direction="up"
        )
        key_stats.append(avg_health_stat)

    return schema.RepoMaintOutput(
        meta=meta,
        headline=headline,
        key_stats=key_stats,
        mode=mode,
        repos=repo_entries,
        actions=all_actions,
    )
