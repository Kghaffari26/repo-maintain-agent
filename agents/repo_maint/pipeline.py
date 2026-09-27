"""End-to-end orchestration (SPEC_REPO_MAINT.md §4), split along the
``agents_core`` agent contract so ``agent.py`` can map it one-to-one:

    fetch_repo_data   network only: every GitHub read for one repo      (Agent.fetch)
    compute_repo      pure Python: every published number, no LLM        (Agent.transform)
    finish_repo       LLM triage + changelog, then plan/gate/execute     (Agent.analyze)

``run`` chains all three over every configured repo and is what the tests drive.
A repo that fails hard (missing, no access, a GitHub error) is logged and left
out of this run's ``repos`` rather than failing every other repo (§6 has no
"failed repo" shape to publish it in); the run fails only if every repo does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from agents_core import tracing
from agents_core.http import HttpError

from agents.repo_maint import actions as actions_mod
from agents.repo_maint import changelog as changelog_mod
from agents.repo_maint import duplicates as duplicates_mod
from agents.repo_maint import fetch as fetch_mod
from agents.repo_maint import health as health_mod
from agents.repo_maint import metrics as metrics_mod
from agents.repo_maint import schema
from agents.repo_maint import stale as stale_mod
from agents.repo_maint import triage as triage_mod
from agents.repo_maint import untriaged as untriaged_mod
from agents.repo_maint.config import Config, RepoConfig, check_write_gates
from agents.repo_maint.gh import GitHubClient, GitHubRequestError, GitHubTokenMissing
from agents.repo_maint.state import RepoState, State, get_repo_state

log = logging.getLogger(__name__)

#: Failures that take one repo out of a run without failing the others.
REPO_FAILURES = (GitHubRequestError, GitHubTokenMissing, fetch_mod.RepoUnavailable, HttpError)

ClassifyFactory = Callable[[RepoConfig, str | None, set[str]], triage_mod.ClassifyFn]


def _issue_url(repo: RepoConfig, number: int) -> str:
    return f"https://github.com/{repo.full_name}/issues/{number}"


def _pr_url(repo: RepoConfig, number: int) -> str:
    return f"https://github.com/{repo.full_name}/pull/{number}"


def _repo_url(repo: RepoConfig) -> str:
    return f"https://github.com/{repo.full_name}"


# -- stage 1: fetch ---------------------------------------------------------------------


@dataclass
class RepoFetch:
    """Everything read from GitHub for one repo, plus the client that read it."""

    repo: RepoConfig
    client: GitHubClient
    snapshot: fetch_mod.RepoSnapshot
    base: changelog_mod.BaseRef
    merged_prs: list[dict[str, Any]]
    compare_commits: list[dict[str, Any]]
    changelog_partial: bool


def fetch_repo_data(
    repo: RepoConfig,
    client: GitHubClient,
    now: datetime,
    max_changelog_items: int = 80,
) -> RepoFetch:
    """All GitHub reads for one repo (§3). Conditional reads keep their ETags and
    bodies in the client's ``cache_dir`` (agents-core ``Http.download``)."""
    snapshot = fetch_mod.fetch_repo(client, repo, now=now)

    default_branch = snapshot.meta.get("default_branch", "main")
    base = changelog_mod.select_base(snapshot.latest_release, snapshot.tags, now)
    merged_prs, changelog_partial = fetch_mod.fetch_merged_prs_since(client, repo, base.date)
    compare_commits: list[dict[str, Any]] = []
    has_prs = any(
        pr.get("merged_at") and (pr.get("base") or {}).get("ref") == default_branch
        for pr in merged_prs
    )
    if not has_prs and base.ref:
        compare_commits = fetch_mod.fetch_compare_commits(client, repo, base.ref, default_branch)
    elif not has_prs:
        # No tag to compare from (§5.5's 30-day base): the branch's commits since then.
        compare_commits = fetch_mod.fetch_commits_since(
            client, repo, default_branch, base.date, max_changelog_items
        )
    return RepoFetch(
        repo=repo,
        client=client,
        snapshot=snapshot,
        base=base,
        merged_prs=merged_prs,
        compare_commits=compare_commits,
        changelog_partial=changelog_partial,
    )


# -- stage 2: compute (deterministic, no LLM) ---------------------------------------------


@dataclass
class RepoWork:
    """Every deterministic §5 result for one repo; the input to ``finish_repo``."""

    fetched: RepoFetch
    untriaged_issues: list[dict[str, Any]]
    untriaged_over_7d: int
    candidates_by_number: dict[int, list[dict[str, Any]]]
    issues_by_number: dict[int, dict[str, Any]]
    stale_items: list[schema.StalePRItem]
    median_hours: float | None
    no_response_count: int
    ci_default_branch: str
    activity: metrics_mod.Activity12w
    changelog_items: list[changelog_mod.ChangelogItem]
    content_source: str
    version_heading: str
    health: health_mod.HealthResult
    days_since_release: int | None
    cached_triage: dict[int, triage_mod.TriageResult] = field(default_factory=dict)

    @property
    def repo(self) -> RepoConfig:
        return self.fetched.repo

    @property
    def needs_fresh_triage(self) -> int:
        return sum(1 for i in self.untriaged_issues if i["number"] not in self.cached_triage)


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


def compute_repo(
    fetched: RepoFetch, repo_state: RepoState, now: datetime, config: Config
) -> RepoWork:
    settings = config.settings
    repo = fetched.repo
    snapshot = fetched.snapshot
    default_branch = snapshot.meta.get("default_branch", "main")

    # -- untriaged + duplicates (§5.1, §5.3) ----------------------------------------------
    untriaged_issues = untriaged_mod.filter_untriaged(
        snapshot.open_issues, snapshot.comments_by_number, repo
    )
    untriaged_over_7d = metrics_mod.untriaged_over_7d(untriaged_issues, now)

    corpus = duplicates_mod.build_corpus(snapshot.open_issues, snapshot.closed_issues_180d)
    by_number = {i["number"]: i for i in [*snapshot.closed_issues_180d, *snapshot.open_issues]}
    dup_index = duplicates_mod.DuplicateIndex(corpus)
    candidates_by_number: dict[int, list[dict[str, Any]]] = {}
    for issue in untriaged_issues:
        scored = dup_index.candidates(issue["number"], threshold=settings.dup_threshold)
        candidates_by_number[issue["number"]] = [
            {
                "number": c["number"],
                "title": c["title"],
                "state": c.get("state", "open"),
                "similarity": round(float(score), 4),
                "snippet": (by_number.get(c["number"], c).get("body") or "")[
                    : triage_mod.MAX_SNIPPET_CHARS
                ],
            }
            for c, score in scored
        ]

    cached_triage: dict[int, triage_mod.TriageResult] = {}
    for issue in untriaged_issues:
        result, cached = triage_mod.triage_issue(
            issue,
            candidates_by_number.get(issue["number"], []),
            snapshot.labels,
            repo,
            repo_state.triage_cache.get(str(issue["number"])),
            classify_fn=None,
        )
        if result is not None and cached:
            cached_triage[issue["number"]] = result

    # -- stale PRs (§5.4) --------------------------------------------------------------------
    stale_items = _build_stale_pr_items(
        snapshot.open_prs,
        snapshot.pr_reviews,
        snapshot.pr_check_runs,
        repo,
        now,
        settings.stale_days,
        settings.include_drafts,
    )

    # -- metrics (§5.2) ------------------------------------------------------------------------
    issues_with_comments = [
        (i, snapshot.comments_by_number.get(i["number"], [])) for i in snapshot.open_issues
    ]
    median_hours, no_response_count = metrics_mod.median_first_response_hours(
        issues_with_comments, now
    )
    ci_default_branch = metrics_mod.ci_state_from_check_runs(snapshot.default_branch_check_runs)
    activity = metrics_mod.activity_12w(snapshot.recent_activity, now)

    # -- changelog contents (§5.5) ----------------------------------------------------------
    base = fetched.base
    items = changelog_mod.select_pull_requests(
        fetched.merged_prs, base, default_branch, settings.max_changelog_items
    )
    content_source = "pull_requests"
    if not items and fetched.compare_commits:
        items = changelog_mod.select_commits(fetched.compare_commits, settings.max_changelog_items)
        content_source = "commits"
    suggested_version = changelog_mod.suggest_version(items, base)
    heading = f"## [{suggested_version}] - Unreleased" if suggested_version else "## Unreleased"

    # -- health (§5.6) ---------------------------------------------------------------------------
    days_since_release = None
    if base.source in ("release", "tag"):
        days_since_release = metrics_mod.days_since(base.date, now)
    health = health_mod.compute_health(
        untriaged_over_7d_count=len(untriaged_over_7d),
        stale_pr_count=len(stale_items),
        median_first_response_hours=median_hours,
        ci_default_branch=ci_default_branch,
        merged_since_release=len(fetched.merged_prs),
        days_since_release=days_since_release,
        community_profile=snapshot.community_profile,
        penalties=config.health.penalties,
    )

    return RepoWork(
        fetched=fetched,
        untriaged_issues=untriaged_issues,
        untriaged_over_7d=len(untriaged_over_7d),
        candidates_by_number=candidates_by_number,
        issues_by_number={i["number"]: i for i in snapshot.open_issues},
        stale_items=stale_items,
        median_hours=median_hours,
        no_response_count=no_response_count,
        ci_default_branch=ci_default_branch,
        activity=activity,
        changelog_items=items,
        content_source=content_source,
        version_heading=heading,
        health=health,
        days_since_release=days_since_release,
        cached_triage=cached_triage,
    )


def planned_actions(work: RepoWork, repo_state: RepoState) -> list[actions_mod.Action]:
    """What apply mode would do from the cached triage alone (``--dry-run``'s listing)."""
    planned: list[actions_mod.Action] = []
    for number, result in work.cached_triage.items():
        issue = work.issues_by_number[number]
        planned += actions_mod.plan_actions_for_issue(
            result,
            work.repo,
            work.fetched.snapshot.comments_by_number.get(number, []),
            set(repo_state.commented),
            work.fetched.snapshot.labels,
            issue_labels={label["name"] for label in issue.get("labels", [])},
        )
    return planned


# -- stage 3: LLM + actions ------------------------------------------------------------------


def _build_triage_items(
    triage_results: list[tuple[triage_mod.TriageResult | None, bool]],
    repo: RepoConfig,
    issues_by_number: dict[int, dict[str, Any]],
) -> list[schema.TriageItem]:
    """Only issues with an actual result are published; the rest (per-run cap,
    budget, a failed model call) are picked up on the next run."""
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


def finish_repo(
    work: RepoWork,
    repo_state: RepoState,
    now: datetime,
    config: Config,
    *,
    apply_flag: bool,
    apply_changes_env: str | None,
    budget: actions_mod.RunBudget,
    classify_factory: ClassifyFactory | None = None,
    draft_fn: changelog_mod.DraftFn | None = None,
    draft_model: str | None = None,
    warnings: list[str] | None = None,
) -> tuple[schema.RepoEntry, list[schema.ActionEntry]]:
    """Triage, changelog and actions for one repo. Non-fatal problems are appended
    to ``warnings`` (published as ``meta.warnings``)."""
    warnings = warnings if warnings is not None else []
    repo = work.repo
    snapshot = work.fetched.snapshot
    client = work.fetched.client

    # -- triage (§7.2): cache hits are free, the rest go to the model ------------------
    classify_fn = None
    if classify_factory is not None:
        classify_fn = classify_factory(repo, snapshot.meta.get("description"), snapshot.labels)
    with tracing.span(
        "custom", f"triage:{repo.full_name}", untriaged=len(work.untriaged_issues)
    ) as sp:
        triage_results = triage_mod.triage_repo(
            work.untriaged_issues,
            work.candidates_by_number,
            snapshot.labels,
            repo,
            repo_state.triage_cache,
            classify_fn,
            config.settings.max_triage_per_repo_per_run,
        )
        sp.set(
            cached=sum(1 for r, cached in triage_results if r is not None and cached),
            fresh=sum(1 for r, cached in triage_results if r is not None and not cached),
            skipped=sum(1 for r, _ in triage_results if r is None),
            model=classify_fn is not None,
        )
    triage_items = _build_triage_items(triage_results, repo, work.issues_by_number)
    not_triaged = sum(1 for result, _ in triage_results if result is None)
    if classify_fn is not None and not_triaged:
        warnings.append(
            f"{repo.full_name}: {not_triaged} of {len(triage_results)} untriaged issues weren't"
            " triaged this run (per-run cap, spend cap or a failed model call); retried next run"
        )
    for issue, (result, _cached) in zip(work.untriaged_issues, triage_results, strict=True):
        if result is not None:
            repo_state.triage_cache[str(issue["number"])] = {
                "hash": triage_mod.content_hash(issue),
                "prompt_version": triage_mod.PROMPT_VERSION,
                "result": asdict(result),
            }
    # Issues that were closed or triaged by a human since don't need their cache entry.
    open_numbers = {str(i["number"]) for i in work.untriaged_issues}
    repo_state.triage_cache = {
        k: v for k, v in repo_state.triage_cache.items() if k in open_numbers
    }

    # -- changelog (§7.3) -------------------------------------------------------------------
    cache = repo_state.changelog_cache
    with tracing.span(
        "custom", f"changelog:{repo.full_name}", items=len(work.changelog_items)
    ) as sp:
        changelog_result = changelog_mod.build_changelog(
            base=work.fetched.base,
            items=work.changelog_items,
            content_source=work.content_source,
            version_heading=work.version_heading,
            cache=cache,
            draft_fn=draft_fn,
        )
        sp.set(
            source=changelog_result.source,
            narrative_source=changelog_result.narrative_source,
            cached=changelog_result.cached,
        )
    if draft_fn is not None and not changelog_result.cacheable:
        warnings.append(
            f"{repo.full_name}: the changelog model call failed; published the template grouping"
        )
    if changelog_result.cached and cache:
        generated_at = metrics_mod.parse_dt(cache.get("generated_at") or now.isoformat())
        model = cache.get("model")
    else:
        generated_at = now
        model = draft_model if changelog_result.narrative_source == "llm" else None
    if changelog_result.cacheable:
        repo_state.changelog_cache = {
            "base_ref": changelog_result.base_ref,
            "pr_set_hash": changelog_mod.pr_set_hash(work.changelog_items),
            "prompt_version": changelog_mod.PROMPT_VERSION,
            "markdown": changelog_result.markdown,
            "narrative_source": changelog_result.narrative_source,
            "model": model,
            "generated_at": schema_iso(generated_at),
        }

    # -- actions: plan -> gate -> execute -> log (§8) ------------------------------------------
    gate = check_write_gates(
        repo,
        apply_flag=apply_flag,
        apply_changes_env=apply_changes_env,
        token_available=True,  # a token was resolved to build this repo's client
    )
    action_entries: list[schema.ActionEntry] = []
    for result, _cached in triage_results:
        if result is None:
            continue
        issue = work.issues_by_number[result.number]
        planned = actions_mod.plan_actions_for_issue(
            result,
            repo,
            snapshot.comments_by_number.get(result.number, []),
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
        partial=snapshot.partial or work.fetched.changelog_partial,
        health=schema.HealthBlock(
            score=work.health.score,
            grade=work.health.grade,
            breakdown=[
                schema.HealthBreakdown(reason=b.reason, points=b.points)
                for b in work.health.breakdown
            ],
        ),
        counts=schema.Counts(
            open_issues=len(snapshot.open_issues),
            untriaged=len(work.untriaged_issues),
            untriaged_over_7d=work.untriaged_over_7d,
            open_prs=len(snapshot.open_prs),
            stale_prs=len(work.stale_items),
            no_response_count=work.no_response_count,
        ),
        median_first_response_hours=work.median_hours,
        ci_default_branch=work.ci_default_branch,
        days_since_release=work.days_since_release,
        activity_12w=schema.Activity12w(
            weeks=work.activity.weeks, opened=work.activity.opened, closed=work.activity.closed
        ),
        triage=triage_items,
        stale_prs=work.stale_items,
        changelog=schema.ChangelogBlock(
            base_ref=changelog_result.base_ref,
            base_date=changelog_result.base_date,
            source=changelog_result.source,
            item_count=changelog_result.item_count,
            suggested_version=changelog_result.suggested_version,
            markdown=changelog_result.markdown,
            narrative_source=changelog_result.narrative_source,
            model=model,
            generated_at=generated_at,
            cached=changelog_result.cached,
        ),
    )
    return repo_entry, action_entries


def schema_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- assembling the §6 body ----------------------------------------------------------------


def _key_stat(
    label: str, value: float, direction: str, previous: dict[str, float] | None
) -> schema.KeyStat:
    """A §6 key stat. ``delta`` is the change since the previous published run's stat
    of the same label (None on a first run); ``delta_format`` is always a standard
    agents-core format."""
    prior = (previous or {}).get(label)
    return schema.KeyStat(
        label=label,
        value=value,
        format="count",
        delta=None if prior is None else value - prior,
        delta_format="count_signed",
        good_direction=direction,
    )


def previous_key_stats(previous_latest: dict[str, Any] | None) -> dict[str, float] | None:
    """``{label: value}`` from the previous ``latest.json``'s key stats, if any."""
    if not previous_latest:
        return None
    stats = {}
    for stat in previous_latest.get("key_stats") or []:
        if isinstance(stat, dict) and isinstance(stat.get("value"), int | float):
            stats[str(stat.get("label"))] = float(stat["value"])
    return stats


def build_body(
    repo_entries: list[schema.RepoEntry],
    all_actions: list[schema.ActionEntry],
    *,
    configured: int,
    previous_stats: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Every top-level §6 field except ``meta`` (agents_core.runner adds that; the
    §6 meta extensions go in ``AgentResult.meta_fields``, see ``RunResult``)."""
    mode: schema.Mode = "apply" if any(a.status == "applied" for a in all_actions) else "report"
    scores = [entry.health.score for entry in repo_entries]
    avg_health = round(sum(scores) / len(scores)) if scores else None
    total_untriaged = sum(entry.counts.untriaged for entry in repo_entries)
    total_triaged = sum(len(entry.triage) for entry in repo_entries)
    total_stale = sum(len(entry.stale_prs) for entry in repo_entries)

    watched = len(repo_entries)
    unreachable = configured - watched
    headline = f"{watched} repo{'s' if watched != 1 else ''} watched"
    if unreachable:
        headline += f" ({unreachable} unreachable)"
    headline += (
        f": {total_triaged} issue{'s' if total_triaged != 1 else ''} triaged, "
        f"{total_untriaged} untriaged, "
        f"{total_stale} stale PR{'s' if total_stale != 1 else ''}."
    )

    key_stats = [_key_stat("Untriaged issues", total_untriaged, "down", previous_stats)]
    if avg_health is not None:
        key_stats.append(_key_stat("Avg health", avg_health, "up", previous_stats))
    key_stats.append(_key_stat("Stale PRs", total_stale, "down", previous_stats))

    return {
        "headline": headline,
        "key_stats": [k.model_dump(mode="json") for k in key_stats],
        "mode": mode,
        "repos": [e.model_dump(mode="json") for e in repo_entries],
        "actions": [a.model_dump(mode="json") for a in all_actions],
    }


# -- the whole thing ---------------------------------------------------------------------------


@dataclass
class RunResult:
    body: dict[str, Any]
    repo_entries: list[schema.RepoEntry]
    actions: list[schema.ActionEntry]
    failed: dict[str, str]
    #: ``RepoMaintMeta`` values (``github_requests``, ``github_304s``), for
    #: ``AgentResult.meta_fields``.
    meta_fields: dict[str, int] = field(default_factory=dict)
    #: Non-fatal problems, for ``meta.warnings`` ("ok with a warning").
    warnings: list[str] = field(default_factory=list)


def fetch_all(
    config: Config,
    state: State,
    build_client: Callable[[RepoConfig], GitHubClient],
    now: datetime,
) -> tuple[list[RepoFetch], dict[str, str]]:
    """Stage 1 over every configured repo, isolating per-repo failures."""
    fetched: list[RepoFetch] = []
    failed: dict[str, str] = {}
    for repo in config.repo:
        try:
            with tracing.span("custom", f"fetch:{repo.full_name}") as sp:
                client = build_client(repo)
                repo_fetch = fetch_repo_data(repo, client, now, config.settings.max_changelog_items)
                sp.set(
                    github_requests=client.requests_made,
                    github_304s=client.not_modified_count,
                    partial=repo_fetch.snapshot.partial or repo_fetch.changelog_partial,
                )
            fetched.append(repo_fetch)
        except REPO_FAILURES as e:
            log.error("%s: skipped this run: %s", repo.full_name, e)
            failed[repo.full_name] = str(e)
    if config.repo and not fetched:
        raise RuntimeError(f"every configured repo failed: {failed}")
    return fetched, failed


def run(
    config: Config,
    state: State,
    build_client: Callable[[RepoConfig], GitHubClient],
    *,
    apply_flag: bool = False,
    apply_changes_env: str | None = None,
    now: datetime | None = None,
    classify_factory: ClassifyFactory | None = None,
    draft_fn: changelog_mod.DraftFn | None = None,
    draft_model: str | None = None,
    previous_stats: dict[str, float] | None = None,
) -> RunResult:
    """Runs every configured repo. Mutates ``state`` in place (caller saves it)."""
    now = now or datetime.now(UTC)
    fetched, failed = fetch_all(config, state, build_client, now)
    works = [compute_repo(f, get_repo_state(state, f.repo.full_name), now, config) for f in fetched]
    return finish_all(
        works,
        config,
        state,
        now,
        failed=failed,
        apply_flag=apply_flag,
        apply_changes_env=apply_changes_env,
        classify_factory=classify_factory,
        draft_fn=draft_fn,
        draft_model=draft_model,
        previous_stats=previous_stats,
    )


def finish_all(
    works: list[RepoWork],
    config: Config,
    state: State,
    now: datetime,
    *,
    failed: dict[str, str],
    apply_flag: bool,
    apply_changes_env: str | None,
    classify_factory: ClassifyFactory | None,
    draft_fn: changelog_mod.DraftFn | None,
    draft_model: str | None,
    previous_stats: dict[str, float] | None = None,
) -> RunResult:
    """Stage 3 over every computed repo, then the §6 body."""
    warnings = [f"{name}: skipped this run ({reason})" for name, reason in failed.items()]
    budget = actions_mod.RunBudget(
        max_writes_per_run=config.settings.max_writes_per_run,
        max_writes_per_repo_per_day=config.settings.max_writes_per_repo_per_day,
    )
    repo_entries: list[schema.RepoEntry] = []
    all_actions: list[schema.ActionEntry] = []
    for work in works:
        with tracing.span("custom", f"repo:{work.repo.full_name}") as sp:
            entry, actions_out = finish_repo(
                work,
                get_repo_state(state, work.repo.full_name),
                now,
                config,
                apply_flag=apply_flag,
                apply_changes_env=apply_changes_env,
                budget=budget,
                classify_factory=classify_factory,
                draft_fn=draft_fn,
                draft_model=draft_model,
                warnings=warnings,
            )
            sp.set(
                health=entry.health.score,
                triaged=len(entry.triage),
                actions=len(actions_out),
                applied=sum(1 for a in actions_out if a.status == "applied"),
            )
        repo_entries.append(entry)
        all_actions.extend(actions_out)

    body = build_body(
        repo_entries,
        all_actions,
        configured=len(config.repo),
        previous_stats=previous_stats,
    )
    meta_fields = {
        "github_requests": sum(w.fetched.client.requests_made for w in works),
        "github_304s": sum(w.fetched.client.not_modified_count for w in works),
    }
    return RunResult(
        body=body,
        repo_entries=repo_entries,
        actions=all_actions,
        failed=failed,
        meta_fields=meta_fields,
        warnings=warnings,
    )
