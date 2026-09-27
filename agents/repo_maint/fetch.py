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

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agents.repo_maint.config import RepoConfig
from agents.repo_maint.gh import GitHubClient, Page, RateLimitLow
from agents.repo_maint.metrics import parse_dt


class RepoUnavailable(RuntimeError):
    """The repo's metadata came back empty (404): missing, renamed, or no access."""


RECENT_ACTIVITY_WEEKS = 12
CLOSED_ISSUES_DUP_WINDOW_DAYS = 180


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
    partial: bool = False


def _owner_repo(full_name: str) -> tuple[str, str]:
    owner, repo = full_name.split("/", 1)
    return owner, repo


def fetch_repo(
    client: GitHubClient,
    repo: RepoConfig,
    *,
    now: datetime,
) -> RepoSnapshot:
    """Fetch everything §3 lists for one repo, degrading to ``partial=True``
    under the rate-limit guard instead of raising. The metadata and list reads
    that repeat every run are conditional (``cache_key``, see ``gh.GitHubClient``)."""
    owner, repo_name = _owner_repo(repo.full_name)
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

    meta_page = safe(lambda: client.get_json(f"/repos/{owner}/{repo_name}", cache_key="meta"))
    meta = (meta_page.items[0] if meta_page and meta_page.items else {}) or {}
    if meta_page is not None and not meta:
        # 404: the repo doesn't exist or this token can't see it. Every other
        # endpoint would 404 too and read as a healthy, empty repo -- fail it instead.
        raise RepoUnavailable(f"{repo.full_name}: not found or not accessible with its token")
    default_branch = meta.get("default_branch", "main")

    labels_page = safe(
        lambda: client.paginate(f"/repos/{owner}/{repo_name}/labels", cache_key="labels")
    )
    labels = {label["name"] for label in (labels_page.items if labels_page else [])}

    open_issues_page = safe(
        lambda: client.paginate(
            f"/repos/{owner}/{repo_name}/issues",
            cache_key="issues_open",
            params={"state": "open", "sort": "updated"},
        )
    )
    open_items = open_issues_page.items if open_issues_page else []
    open_issues = [i for i in open_items if "pull_request" not in i]

    since_12w = (now - timedelta(weeks=RECENT_ACTIVITY_WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_page = safe(
        lambda: client.paginate(
            f"/repos/{owner}/{repo_name}/issues",
            cache_key="issues_recent",
            params={"state": "all", "since": since_12w, "sort": "updated"},
        )
    )
    recent_activity = recent_page.items if recent_page else []

    since_180d_dt = now - timedelta(days=CLOSED_ISSUES_DUP_WINDOW_DAYS)
    since_180d = since_180d_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_page = safe(
        lambda: client.paginate(
            f"/repos/{owner}/{repo_name}/issues",
            cache_key="issues_closed",
            params={"state": "closed", "since": since_180d, "sort": "updated"},
        )
    )
    closed_items = closed_page.items if closed_page else []
    closed_issues_180d = [i for i in closed_items if "pull_request" not in i]

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
            cache_key="pulls_open",
            params={"state": "open", "sort": "updated", "direction": "desc"},
        )
    )
    open_prs = open_prs_page.items if open_prs_page else []

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


def fetch_commits_since(
    client: GitHubClient, repo: RepoConfig, branch: str, since: datetime, max_items: int
) -> list[dict[str, Any]]:
    """Commits on ``branch`` since ``since``, oldest first, for the changelog when a
    repo has no release/tag (so no ``compare`` base) and no merged PRs (§5.5: the
    "last 30 days" base). One page of up to 100, which covers ``max_changelog_items``."""
    owner, repo_name = _owner_repo(repo.full_name)
    try:
        page = client.get_json(
            f"/repos/{owner}/{repo_name}/commits",
            params={
                "sha": branch,
                "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "per_page": min(max(max_items, 1), 100),
            },
        )
    except RateLimitLow:
        return []
    return list(reversed(page.items))
