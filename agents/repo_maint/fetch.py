"""All GitHub reads for one repo, assembled into a ``RepoSnapshot`` (SPEC_REPO_MAINT.md §3, §4).

Stops and marks ``partial=True`` the moment the injected client's rate-limit
guard trips (``gh.RateLimitLow``), returning whatever was already fetched --
never raising out of a run over one repo's data being incomplete (§3: "stop
fetching, publish what you have, and mark the repo partial: true").

Scoping simplification (documented, not in the spec): the first-response-time
metric only considers **open** issues created in the last 90 days, since
comments are only fetched for open issues (needed anyway for untriaged
detection and the marker check) rather than for every issue in a 90-day
window regardless of state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agents.repo_maint.config import RepoConfig
from agents.repo_maint.gh import GitHubClient, Page, RateLimitLow
from agents.repo_maint.metrics import parse_dt

RECENT_ACTIVITY_WEEKS = 12
CLOSED_ISSUES_DUP_WINDOW_DAYS = 180


@dataclass
class RepoEtags:
    """Per-repo ETags carried in state.json, one per cached list endpoint."""

    meta: str | None = None
    labels: str | None = None
    issues_open: str | None = None
    issues_recent: str | None = None
    issues_closed: str | None = None
    pulls_open: str | None = None


@dataclass
class RepoSnapshot:
    repo: RepoConfig
    meta: dict[str, Any]
    labels: set[str]
    open_issues: list[dict[str, Any]]
    recent_activity: list[dict[str, Any]]
    closed_issues_180d: list[dict[str, Any]]
    comments_by_number: dict[int, list[dict[str, Any]]]
    open_prs: list[dict[str, Any]]
    pr_reviews: dict[int, list[dict[str, Any]]]
    pr_check_runs: dict[str, list[dict[str, Any]]]
    default_branch_check_runs: list[dict[str, Any]]
    latest_release: dict[str, Any] | None
    tags: list[dict[str, Any]]
    merged_prs_since_base: list[dict[str, Any]]
    compare_commits: list[dict[str, Any]]
    community_profile: dict[str, Any] | None
    etags: RepoEtags = field(default_factory=RepoEtags)
    partial: bool = False


def _owner_repo(full_name: str) -> tuple[str, str]:
    owner, repo = full_name.split("/", 1)
    return owner, repo


def fetch_repo(
    client: GitHubClient,
    repo: RepoConfig,
    *,
    now: datetime,
    prior_etags: RepoEtags | None = None,
) -> RepoSnapshot:
    """Fetch everything §3 lists for one repo, degrading to ``partial=True``
    under the rate-limit guard instead of raising."""
    owner, repo_name = _owner_repo(repo.full_name)
    etags = prior_etags or RepoEtags()
    partial = False

    def safe(fn):
        nonlocal partial
        if partial:
            return None
        try:
            return fn()
        except RateLimitLow:
            partial = True
            return None

    meta_page = safe(lambda: client.get_json(f"/repos/{owner}/{repo_name}", etag=etags.meta))
    meta = (meta_page.items[0] if meta_page and meta_page.items else {}) or {}
    if meta_page and meta_page.etag:
        etags.meta = meta_page.etag
    default_branch = meta.get("default_branch", "main")

    labels_page = safe(
        lambda: client.paginate(f"/repos/{owner}/{repo_name}/labels", etag=etags.labels)
    )
    labels = {label["name"] for label in (labels_page.items if labels_page else [])}
    if labels_page and labels_page.etag:
        etags.labels = labels_page.etag

    open_issues_page = safe(
        lambda: client.paginate(
            f"/repos/{owner}/{repo_name}/issues",
            etag=etags.issues_open,
            params={"state": "open", "sort": "updated"},
        )
    )
    open_items = open_issues_page.items if open_issues_page else []
    open_issues = [i for i in open_items if "pull_request" not in i]
    if open_issues_page and open_issues_page.etag:
        etags.issues_open = open_issues_page.etag

    since_12w = (now - timedelta(weeks=RECENT_ACTIVITY_WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_page = safe(
        lambda: client.paginate(
            f"/repos/{owner}/{repo_name}/issues",
            etag=etags.issues_recent,
            params={"state": "all", "since": since_12w},
        )
    )
    recent_activity = recent_page.items if recent_page else []
    if recent_page and recent_page.etag:
        etags.issues_recent = recent_page.etag

    since_180d_dt = now - timedelta(days=CLOSED_ISSUES_DUP_WINDOW_DAYS)
    since_180d = since_180d_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_page = safe(
        lambda: client.paginate(
            f"/repos/{owner}/{repo_name}/issues",
            etag=etags.issues_closed,
            params={"state": "closed", "since": since_180d},
        )
    )
    closed_items = closed_page.items if closed_page else []
    closed_issues_180d = [i for i in closed_items if "pull_request" not in i]
    if closed_page and closed_page.etag:
        etags.issues_closed = closed_page.etag

    comments_by_number: dict[int, list[dict[str, Any]]] = {}
    for issue in open_issues:
        comments_page = safe(
            lambda n=issue["number"]: client.paginate(
                f"/repos/{owner}/{repo_name}/issues/{n}/comments"
            )
        )
        comments_by_number[issue["number"]] = comments_page.items if comments_page else []

    open_prs_page = safe(
        lambda: client.paginate(
            f"/repos/{owner}/{repo_name}/pulls",
            etag=etags.pulls_open,
            params={"state": "open"},
        )
    )
    open_prs = open_prs_page.items if open_prs_page else []
    if open_prs_page and open_prs_page.etag:
        etags.pulls_open = open_prs_page.etag

    pr_reviews: dict[int, list[dict[str, Any]]] = {}
    pr_check_runs: dict[str, list[dict[str, Any]]] = {}
    for pr in open_prs:
        reviews_page = safe(
            lambda n=pr["number"]: client.paginate(f"/repos/{owner}/{repo_name}/pulls/{n}/reviews")
        )
        pr_reviews[pr["number"]] = reviews_page.items if reviews_page else []
        head_sha = (pr.get("head") or {}).get("sha")
        if head_sha:
            checks_page = safe(
                lambda sha=head_sha: client.get_json(
                    f"/repos/{owner}/{repo_name}/commits/{sha}/check-runs"
                )
            )
            body = checks_page.items[0] if checks_page and checks_page.items else {}
            pr_check_runs[head_sha] = (body or {}).get("check_runs", [])

    # GitHub's check-runs endpoint accepts a branch name as `ref` directly,
    # so no separate lookup of the default branch's head sha is needed.
    default_branch_check_runs: list[dict[str, Any]] = []
    checks_page = safe(
        lambda: client.get_json(f"/repos/{owner}/{repo_name}/commits/{default_branch}/check-runs")
    )
    if checks_page and checks_page.items:
        default_branch_check_runs = (checks_page.items[0] or {}).get("check_runs", [])

    release_page = safe(lambda: client.get_json(f"/repos/{owner}/{repo_name}/releases/latest"))
    latest_release = release_page.items[0] if release_page and release_page.items else None

    tags_page = safe(lambda: client.paginate(f"/repos/{owner}/{repo_name}/tags"))
    tags = tags_page.items if tags_page else []

    community_page = safe(
        lambda: client.get_json(f"/repos/{owner}/{repo_name}/community/profile")
    )
    community_profile = community_page.items[0] if community_page and community_page.items else None

    return RepoSnapshot(
        repo=repo,
        meta=meta,
        labels=labels,
        open_issues=open_issues,
        recent_activity=recent_activity,
        closed_issues_180d=closed_issues_180d,
        comments_by_number=comments_by_number,
        open_prs=open_prs,
        pr_reviews=pr_reviews,
        pr_check_runs=pr_check_runs,
        default_branch_check_runs=default_branch_check_runs,
        latest_release=latest_release,
        tags=tags,
        merged_prs_since_base=[],  # filled in by fetch_merged_prs_since once base is known (§5.5)
        compare_commits=[],
        community_profile=community_profile,
        etags=etags,
        partial=partial,
    )


def fetch_merged_prs_since(
    client: GitHubClient, repo: RepoConfig, since: datetime
) -> tuple[list[dict[str, Any]], bool]:
    """Merged PRs since ``since``, paginated until ``updated_at < since`` (§3, §5.5 step 2).

    Returns ``(prs, partial)``.
    """
    owner, repo_name = _owner_repo(repo.full_name)
    merged: list[dict[str, Any]] = []
    page_number = 1
    partial = False
    while True:
        try:
            page: Page = client.get_json(
                f"/repos/{owner}/{repo_name}/pulls",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page_number,
                },
            )
        except RateLimitLow:
            partial = True
            break
        if not page.items:
            break
        for pr in page.items:
            if pr.get("merged_at") and parse_dt(pr["merged_at"]) > since:
                merged.append(pr)
        oldest_updated = parse_dt(page.items[-1]["updated_at"])
        if oldest_updated < since:
            break
        page_number += 1
    return merged, partial


def fetch_compare_commits(
    client: GitHubClient, repo: RepoConfig, base_ref: str, head_ref: str
) -> list[dict[str, Any]]:
    """Commits since a ref, for repos with no merged PRs (§3, §5.5 step 2)."""
    owner, repo_name = _owner_repo(repo.full_name)
    try:
        page = client.get_json(f"/repos/{owner}/{repo_name}/compare/{base_ref}...{head_ref}")
    except RateLimitLow:
        return []
    body = page.items[0] if page.items else {}
    return (body or {}).get("commits", [])
