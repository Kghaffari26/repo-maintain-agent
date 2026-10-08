"""Thin GitHub REST client for the repo maintenance agent (SPEC_REPO_MAINT.md §3, §8.2).

This module has **no networking implementation of its own**: every request goes
through an injected ``agents_core.http.Http`` (retries, backoff, secret-safe
logging, per-host caps). Conditional reads (ETags) use agents-core's
``Http.download`` helper, which keeps the last body and its ETag on disk under the
client's ``cache_dir`` and answers a 304 from there. Every other read passes
``ttl_seconds=0`` so agents-core's 6-hour dev cache never serves stale issue data.

The client exposes **exactly three** write operations: ``add_labels`` and
``add_comment`` (§8.2), and ``create_draft_pr`` (§6.1), which is marked
``requires_approval`` and refuses to run without a passing
``config.FixPRApproval`` for that exact proposal. There are no other write
endpoints here on purpose, so the rest of the agent cannot express any write the
spec doesn't allow -- see ``test_gh_client.py::test_only_three_write_methods_exist``.
Nothing here can merge a pull request or enable auto-merge.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents_core.http import Http, HttpError, parse_link_header

from agents.repo_maint.config import FixPRApproval

GITHUB_API_BASE = "https://api.github.com"
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"

#: Stop fetching once the primary rate limit drops below this (§3).
RATE_LIMIT_FLOOR = 100

#: Repo config `token = "..."` values, mapped to the env var holding the token.
TOKEN_ENV_VARS = {
    "default": "GITHUB_TOKEN",
    "repo_maint": "REPO_MAINT_TOKEN",
}

#: Statuses that mean "nothing here" rather than a failed run: 404 (no release,
#: no community profile, ...) and 409 (commit endpoints on an empty repository).
EMPTY_STATUSES = frozenset({404, 409})

#: Conditional-read cache keys become file names under ``cache_dir``.
_CACHE_KEY = re.compile(r"^[a-z0-9_]+$")


#: Every fix-proposal branch starts with this, so they're recognizable and never
#: collide with a human's branch.
FIX_BRANCH_PREFIX = "repo-maint/fix-"
#: Required at the top of every fix-proposal PR body.
DRAFT_PR_BANNER = "Proposed by repo-maint agent; needs human review."


def requires_approval[F](fn: F) -> F:
    """Marks a write that needs a human's approval of the specific change, on top of
    the five §8.1 write gates (checked in code and by the introspection tests)."""
    fn.requires_approval = True  # type: ignore[attr-defined]
    return fn


@dataclass(frozen=True)
class FileChange:
    """One file's full new content on the fix branch. ``blob_sha`` is the current
    blob's sha (the Contents API needs it to update a file); None creates the file."""

    path: str
    content: str
    blob_sha: str | None


@dataclass(frozen=True)
class DraftPR:
    proposal_id: str
    base_branch: str
    base_sha: str
    head_branch: str
    title: str
    body: str
    files: tuple[FileChange, ...]


class RateLimitLow(RuntimeError):
    """Raised when the primary GitHub rate limit drops below ``RATE_LIMIT_FLOOR``."""


class GitHubTokenMissing(RuntimeError):
    """Raised when the env var for a repo's configured token isn't set."""


class GitHubRequestError(RuntimeError):
    """Raised when a GitHub request returns an unexpected error status."""


def resolve_token(token_name: str) -> str:
    """Resolve a repo config's ``token`` field ("default" | "repo_maint") to a value.

    "default" reads ``GITHUB_TOKEN`` (the Actions-provided token, scoped to
    the current repo); "repo_maint" reads ``REPO_MAINT_TOKEN`` (a
    fine-grained PAT for repos the current run doesn't own by default).
    """
    try:
        env_var = TOKEN_ENV_VARS[token_name]
    except KeyError:
        raise ValueError(
            f"unknown token name {token_name!r}; expected one of {sorted(TOKEN_ENV_VARS)}"
        ) from None
    value = os.environ.get(env_var)
    if not value:
        raise GitHubTokenMissing(f"{env_var} is not set")
    return value


def default_headers(token: str) -> dict[str, str]:
    """Headers sent with every GitHub request (§3)."""
    return {
        "Accept": GITHUB_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "Authorization": f"Bearer {token}",
    }


@dataclass
class Page:
    """The result of a (possibly paginated) GitHub list read."""

    items: list[dict[str, Any]]
    etag: str | None
    not_modified: bool


@dataclass
class _Result:
    status: int
    headers: dict[str, str]
    body: Any


@dataclass
class _Download:
    """A conditional read's outcome: the body (the previous one on a 304), its ETag,
    and the ``Link`` header's ``rel="next"`` URL from this response (on a 304 too)."""

    body: Any
    etag: str | None
    not_modified: bool
    next_url: str | None


def _url(path: str) -> str:
    return path if path.startswith("http") else f"{GITHUB_API_BASE}{path}"


def _as_items(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return body
    if body is None:
        return []
    return [body]


class GitHubClient:
    """A per-(repo, token) GitHub REST client over an injected ``agents_core.http.Http``.

    ``cache_dir`` holds the conditional-read cache: ``get_json``/``paginate`` calls
    given a ``cache_key`` go through ``Http.download`` to ``<cache_dir>/<key>.json``
    (with its ``.meta.json`` ETag sidecar), so an unchanged collection costs one 304
    and is read back from disk. Without a ``cache_dir`` every read is unconditional.
    """

    def __init__(self, http: Http, token: str, cache_dir: Path | str | None = None) -> None:
        self._http = http
        self._headers = default_headers(token)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.rate_limit_remaining: int | None = None
        self.requests_made = 0
        self.not_modified_count = 0

    # -- internals ------------------------------------------------------

    def _check_rate_limit(self) -> None:
        if self.rate_limit_remaining is not None and self.rate_limit_remaining < RATE_LIMIT_FLOOR:
            raise RateLimitLow(
                f"x-ratelimit-remaining {self.rate_limit_remaining} < floor {RATE_LIMIT_FLOOR}"
            )

    def _record(self, headers: dict[str, str]) -> None:
        self.requests_made += 1
        remaining = headers.get("x-ratelimit-remaining")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> _Result:
        self._check_rate_limit()
        try:
            response = self._http.request(
                "GET", _url(path), params=params, headers=self._headers, ttl_seconds=0
            )
        except HttpError as e:
            # agents_core.http raises on every non-2xx; 404/409 are answers, not failures.
            self._record({})
            if e.status in EMPTY_STATUSES:
                return _Result(e.status, {}, None)
            raise GitHubRequestError(f"GET {path} failed: {e}") from e
        self._record(response.headers)
        return _Result(response.status, response.headers, response.json())

    def _cache_path(self, cache_key: str) -> Path:
        assert self.cache_dir is not None
        if not _CACHE_KEY.match(cache_key):
            raise ValueError(f"invalid cache key {cache_key!r}")
        return self.cache_dir / f"{cache_key}.json"

    def _download(
        self, path: str, cache_key: str, params: dict[str, Any] | None, *, force: bool = False
    ) -> _Download | None:
        """A conditional GET through ``Http.download``, or None for a 404/409 ("nothing
        here"). Its response headers (a 304's too) update the rate-limit tracking."""
        self._check_rate_limit()
        dest = self._cache_path(cache_key)
        try:
            result = self._http.download(
                _url(path), dest, params=params, headers=self._headers, force=force
            )
        except HttpError as e:
            self._record({})
            if e.status in EMPTY_STATUSES:
                return None
            raise GitHubRequestError(f"GET {path} failed: {e}") from e
        self._record(result.headers)
        if not result.modified:
            self.not_modified_count += 1
        try:
            body = json.loads(dest.read_text())
        except (OSError, json.JSONDecodeError):
            if force:
                raise GitHubRequestError(f"GET {path}: unreadable response body") from None
            return self._download(path, cache_key, params, force=True)
        return _Download(body, result.etag, not result.modified, result.links.get("next"))

    # -- reads ------------------------------------------------------------

    def get_json(
        self, path: str, *, params: dict[str, Any] | None = None, cache_key: str | None = None
    ) -> Page:
        """A single GET; conditional (ETag) when a ``cache_key`` and ``cache_dir`` are
        set, in which case a 304's ``items`` are the previous body."""
        if cache_key is None or self.cache_dir is None:
            result = self._get(path, params=params)
            if result.status in EMPTY_STATUSES:
                return Page(items=[], etag=None, not_modified=False)
            return Page(
                items=_as_items(result.body), etag=result.headers.get("etag"), not_modified=False
            )
        got = self._download(path, cache_key, params)
        if got is None:
            return Page(items=[], etag=None, not_modified=False)
        return Page(items=_as_items(got.body), etag=got.etag, not_modified=got.not_modified)

    def paginate(
        self, path: str, *, params: dict[str, Any] | None = None, cache_key: str | None = None
    ) -> Page:
        """Every page of a list endpoint.

        With a ``cache_key``, the first page is a conditional GET: a 304 there means
        the whole collection is unchanged (these endpoints are polled sorted by
        ``updated``), and all pages are read back from ``<key>.pages.json``, which
        records the first page's ETag it belongs to. Otherwise the first page is
        re-read and the rest follow its ``Link: rel="next"`` headers (unconditional
        reads, like every page without a ``cache_key``).
        """
        page_params: dict[str, Any] = dict(params or {})
        page_params.setdefault("per_page", 100)
        if cache_key is None or self.cache_dir is None:
            return self._paginate_links(path, page_params)

        pages_path = self._cache_path(cache_key).with_suffix(".pages.json")
        got = self._download(path, cache_key, page_params)
        if got is None:
            return Page(items=[], etag=None, not_modified=False)
        if got.not_modified:
            saved = _read_pages(pages_path)
            if saved is not None and saved.get("etag") == got.etag:
                return Page(items=saved["items"], etag=got.etag, not_modified=True)
            # The pages file is missing or belongs to another ETag: fetch it all again.
            got = self._download(path, cache_key, page_params, force=True)
            if got is None:
                return Page(items=[], etag=None, not_modified=False)

        items = list(got.body) if isinstance(got.body, list) else []
        items.extend(self._follow_next(got.next_url))
        pages_path.write_text(json.dumps({"etag": got.etag, "items": items}))
        return Page(items=items, etag=got.etag, not_modified=False)

    def _paginate_links(self, path: str, page_params: dict[str, Any]) -> Page:
        result = self._get(path, params=page_params)
        if result.status in EMPTY_STATUSES:
            return Page(items=[], etag=None, not_modified=False)
        items = list(result.body) if isinstance(result.body, list) else []
        items.extend(self._follow_next(_next_link(result.headers)))
        return Page(items=items, etag=result.headers.get("etag"), not_modified=False)

    def _follow_next(self, next_url: str | None) -> list[dict[str, Any]]:
        """Every item on the pages after the first, following ``Link: rel="next"``."""
        items: list[dict[str, Any]] = []
        while next_url:
            result = self._get(next_url)
            if result.status in EMPTY_STATUSES:
                break
            if isinstance(result.body, list):
                items.extend(result.body)
            next_url = _next_link(result.headers)
        return items

    # -- writes: the ONLY three write operations this client exposes ------

    def add_labels(self, owner: str, repo: str, issue_number: int, labels: Iterable[str]) -> Any:
        """``POST /repos/{owner}/{repo}/issues/{issue_number}/labels`` (§8.2)."""
        self._check_rate_limit()
        try:
            response = self._http.request(
                "POST",
                _url(f"/repos/{owner}/{repo}/issues/{issue_number}/labels"),
                headers=self._headers,
                json_body={"labels": list(labels)},
                ttl_seconds=0,
            )
        except HttpError as e:
            raise GitHubRequestError(f"add_labels failed: {e}") from e
        self._record(response.headers)
        return response.json()

    def add_comment(self, owner: str, repo: str, issue_number: int, body: str) -> Any:
        """``POST /repos/{owner}/{repo}/issues/{issue_number}/comments`` (§8.2)."""
        self._check_rate_limit()
        try:
            response = self._http.request(
                "POST",
                _url(f"/repos/{owner}/{repo}/issues/{issue_number}/comments"),
                headers=self._headers,
                json_body={"body": body},
                ttl_seconds=0,
            )
        except HttpError as e:
            raise GitHubRequestError(f"add_comment failed: {e}") from e
        self._record(response.headers)
        return response.json()

    @requires_approval
    def create_draft_pr(
        self, owner: str, repo: str, pr: DraftPR, approval: FixPRApproval
    ) -> dict[str, Any]:
        """Open a **draft** pull request for one human-approved fix proposal (§6.1):
        ``POST /git/refs`` (a new ``repo-maint/fix-*`` branch at ``base_sha``), one
        ``PUT /contents/{path}`` per changed file on that branch, then ``POST /pulls``
        with ``draft: true``. Never merges, never enables auto-merge.

        Refuses (``PermissionError``) unless ``approval`` passed for this repo and
        this proposal id -- which takes all five write gates, ``allow_fix_prs``, role
        ``sandbox`` and a human approval (``config.approve_fix_pr``).
        """
        _check_draft_pr(owner, repo, pr, approval)
        self._check_rate_limit()
        try:
            response = self._http.request(
                "POST",
                _url(f"/repos/{owner}/{repo}/git/refs"),
                headers=self._headers,
                json_body={"ref": f"refs/heads/{pr.head_branch}", "sha": pr.base_sha},
                ttl_seconds=0,
            )
            self._record(response.headers)
            for change in pr.files:
                payload: dict[str, Any] = {
                    "message": f"repo-maint fix proposal {pr.proposal_id}: {change.path}",
                    "content": base64.b64encode(change.content.encode()).decode(),
                    "branch": pr.head_branch,
                }
                if change.blob_sha:
                    payload["sha"] = change.blob_sha
                response = self._http.request(
                    "PUT",
                    _url(f"/repos/{owner}/{repo}/contents/{change.path}"),
                    headers=self._headers,
                    json_body=payload,
                    ttl_seconds=0,
                )
                self._record(response.headers)
            response = self._http.request(
                "POST",
                _url(f"/repos/{owner}/{repo}/pulls"),
                headers=self._headers,
                json_body={
                    "title": pr.title,
                    "head": pr.head_branch,
                    "base": pr.base_branch,
                    "body": pr.body,
                    "draft": True,
                    "maintainer_can_modify": True,
                },
                ttl_seconds=0,
            )
        except HttpError as e:
            raise GitHubRequestError(f"create_draft_pr failed: {e}") from e
        self._record(response.headers)
        return response.json()


def _read_pages(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def _check_draft_pr(owner: str, repo: str, pr: DraftPR, approval: FixPRApproval) -> None:
    if not (
        isinstance(approval, FixPRApproval)
        and approval.passed
        and approval.repo == f"{owner}/{repo}"
        and approval.proposal_id == pr.proposal_id
    ):
        raise PermissionError(
            f"create_draft_pr needs a passing FixPRApproval for {owner}/{repo} proposal"
            f" {pr.proposal_id}; got {approval!r}"
        )
    if not pr.head_branch.startswith(FIX_BRANCH_PREFIX):
        raise ValueError(f"fix branches must start with {FIX_BRANCH_PREFIX!r}")
    if DRAFT_PR_BANNER not in pr.body:
        raise ValueError("a fix-proposal PR body must carry the human-review banner")
    if not pr.files:
        raise ValueError("a fix-proposal PR needs at least one file change")


def _next_link(headers: dict[str, str]) -> str | None:
    """The ``Link`` header's ``rel="next"`` URL, if any (agents-core's parser)."""
    return parse_link_header(headers.get("link", "")).get("next")
