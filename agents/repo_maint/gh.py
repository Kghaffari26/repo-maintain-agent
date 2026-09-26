"""Thin GitHub REST client for the repo maintenance agent (SPEC_REPO_MAINT.md §3, §8.2).

This module has **no networking implementation of its own**: every request goes
through an injected ``agents_core.http.Http`` (retries, backoff, secret-safe
logging). GitHub reads always pass ``ttl_seconds=0`` so agents_core's on-disk
dev cache never serves stale issue data; this client does GitHub's own
conditional requests (ETags) instead.

The client exposes **exactly two** write operations (``add_labels`` and
``add_comment``). There are no other write endpoints here on purpose, so the
rest of the agent cannot express any write the spec doesn't allow -- see
``test_gh_client.py::test_only_two_write_methods_exist``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agents_core.http import Http, HttpError

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

    ``etag_bodies`` maps an ETag to the items it was served with. A conditional
    request is only sent when that body is known, so a 304 always yields the
    real (unchanged) data rather than an empty list. The caller persists the
    mapping in ``data/repo_maint/state.json`` next to the ETags themselves.
    """

    def __init__(
        self,
        http: Http,
        token: str,
        etag_bodies: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._http = http
        self._headers = default_headers(token)
        self.etag_bodies = etag_bodies if etag_bodies is not None else {}
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

    def _get(
        self, path: str, *, etag: str | None = None, params: dict[str, Any] | None = None
    ) -> _Result:
        self._check_rate_limit()
        headers = dict(self._headers)
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = self._http.request(
                "GET", _url(path), params=params, headers=headers, ttl_seconds=0
            )
        except HttpError as e:
            # agents_core.http raises on every non-2xx; 304/404/409 are answers, not failures.
            self._record({})
            if e.status == 304:
                self.not_modified_count += 1
                return _Result(304, {}, None)
            if e.status in EMPTY_STATUSES:
                return _Result(e.status, {}, None)
            raise GitHubRequestError(f"GET {path} failed: {e}") from e
        self._record(response.headers)
        return _Result(response.status, response.headers, response.json())

    def _conditional_etag(self, etag: str | None) -> str | None:
        return etag if etag and etag in self.etag_bodies else None

    # -- reads ------------------------------------------------------------

    def get_json(
        self, path: str, *, etag: str | None = None, params: dict[str, Any] | None = None
    ) -> Page:
        """A single conditional GET. On a 304, ``items`` are the cached body."""
        etag = self._conditional_etag(etag)
        result = self._get(path, etag=etag, params=params)
        if result.status == 304 and etag:
            return Page(items=self.etag_bodies[etag], etag=etag, not_modified=True)
        if result.status in EMPTY_STATUSES:
            return Page(items=[], etag=None, not_modified=False)
        items = _as_items(result.body)
        new_etag = result.headers.get("etag")
        if new_etag:
            self.etag_bodies[new_etag] = items
        return Page(items=items, etag=new_etag, not_modified=False)

    def paginate(
        self, path: str, *, etag: str | None = None, params: dict[str, Any] | None = None
    ) -> Page:
        """Follow ``Link: rel="next"`` headers, collecting every page's items.

        The conditional request is only made on the first page: a 304 there
        means the whole collection is unchanged, since these list endpoints
        are polled sorted by ``updated``.
        """
        etag = self._conditional_etag(etag)
        all_items: list[dict[str, Any]] = []
        page_params: dict[str, Any] = dict(params or {})
        page_params.setdefault("per_page", 100)

        next_path: str | None = path
        first_page_etag: str | None = None
        first = True
        while next_path:
            result = self._get(
                next_path,
                etag=etag if first else None,
                params=page_params if first else None,
            )
            if first and result.status == 304 and etag:
                return Page(items=self.etag_bodies[etag], etag=etag, not_modified=True)
            if result.status in EMPTY_STATUSES:
                if first:
                    return Page(items=[], etag=None, not_modified=False)
                break
            if isinstance(result.body, list):
                all_items.extend(result.body)
            if first:
                first_page_etag = result.headers.get("etag")
            next_path = _next_link(result.headers.get("link"))
            first = False

        if first_page_etag:
            self.etag_bodies[first_page_etag] = all_items
        return Page(items=all_items, etag=first_page_etag, not_modified=False)

    # -- writes: the ONLY two write operations this client exposes --------

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


def _next_link(link_header: str | None) -> str | None:
    """Parse a GitHub ``Link`` header and return the ``rel="next"`` URL, if any."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if 'rel="next"' not in segment:
            continue
        start = segment.find("<")
        end = segment.find(">")
        if start != -1 and end != -1:
            return segment[start + 1 : end]
    return None
