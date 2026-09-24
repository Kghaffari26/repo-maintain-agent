"""Thin GitHub REST client for the repo maintenance agent (SPEC_REPO_MAINT.md §3, §8.2).

This module has **no networking implementation of its own** -- per tonight's
hard rule ("never write your own http module"), it takes an already-built
HTTP client injected by the caller and only knows how to build GitHub
requests and interpret responses. In production that injected client is
``agents_core.http.HttpClient``; until that package is installable (see
STATUS.md, "Needed from agents-core"), tests and the one-off report run
inject a small local stand-in instead (``tests/repo_maint/fakes.py`` /
``scripts/run_report_once.py``).

The client exposes **exactly two** write operations (``add_labels`` and
``add_comment``). There are no other write endpoints here on purpose, so
the rest of the agent cannot express any write the spec doesn't allow --
see ``test_gh_client.py::test_only_two_write_methods_exist``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

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
    """The headers a caller should construct its injected HTTP client with."""
    return {
        "Accept": GITHUB_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "Authorization": f"Bearer {token}",
    }


class HttpResponseLike(Protocol):
    """The minimal response shape this module needs. ``agents_core.http``'s
    response type satisfies this structurally, as does any test fake."""

    status_code: int
    headers: Any  # a mapping-like object with a case-insensitive .get(name)
    json_body: Any
    text: str


class HttpClientLike(Protocol):
    """The minimal client shape this module needs from an injected HTTP client."""

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponseLike: ...


@dataclass
class Page:
    """The result of a (possibly paginated) GitHub list read."""

    items: list[dict[str, Any]]
    etag: str | None
    not_modified: bool


class GitHubClient:
    """A per-(repo, token) GitHub REST client wrapping an injected HTTP client.

    ETags are passed in and returned by the caller rather than cached
    internally, so ``data/repo_maint/state.json`` stays the single source
    of truth for what's cached.
    """

    def __init__(self, http: HttpClientLike) -> None:
        self._http = http
        self.rate_limit_remaining: int | None = None
        self.requests_made = 0
        self.not_modified_count = 0

    # -- internals ------------------------------------------------------

    def _check_rate_limit(self) -> None:
        if self.rate_limit_remaining is not None and self.rate_limit_remaining < RATE_LIMIT_FLOOR:
            raise RateLimitLow(
                f"x-ratelimit-remaining {self.rate_limit_remaining} < floor {RATE_LIMIT_FLOOR}"
            )

    def _record_rate_limit(self, response: HttpResponseLike) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)

    def _get(
        self, path: str, *, etag: str | None = None, params: dict[str, Any] | None = None
    ) -> HttpResponseLike:
        self._check_rate_limit()
        headers = {"If-None-Match": etag} if etag else {}
        response = self._http.request("GET", path, params=params, headers=headers)
        self.requests_made += 1
        self._record_rate_limit(response)
        if response.status_code == 304:
            self.not_modified_count += 1
        return response

    # -- reads ------------------------------------------------------------

    def get_json(
        self, path: str, *, etag: str | None = None, params: dict[str, Any] | None = None
    ) -> Page:
        """A single conditional GET. ``not_modified=True`` on a 304."""
        response = self._get(path, etag=etag, params=params)
        if response.status_code == 304:
            return Page(items=[], etag=etag, not_modified=True)
        if response.status_code == 404:
            return Page(items=[], etag=None, not_modified=False)
        if response.status_code >= 400:
            raise GitHubRequestError(f"GET {path} failed: {response.status_code} {response.text}")
        body = response.json_body
        if isinstance(body, list):
            items = body
        elif body is None:
            items = []
        else:
            items = [body]
        return Page(items=items, etag=response.headers.get("etag"), not_modified=False)

    def paginate(
        self, path: str, *, etag: str | None = None, params: dict[str, Any] | None = None
    ) -> Page:
        """Follow ``Link: rel="next"`` headers, collecting every page's items.

        The conditional request is only made on the first page: a 304 there
        means the whole collection is unchanged, since these list endpoints
        are polled sorted by ``updated``/``since``.
        """
        all_items: list[dict[str, Any]] = []
        page_params: dict[str, Any] = dict(params or {})
        page_params.setdefault("per_page", 100)

        next_path: str | None = path
        first_page_etag: str | None = None
        first = True
        while next_path:
            response = self._get(
                next_path,
                etag=etag if first else None,
                params=page_params if first else None,
            )
            if first and response.status_code == 304:
                return Page(items=[], etag=etag, not_modified=True)
            if response.status_code >= 400:
                raise GitHubRequestError(
                    f"GET {next_path} failed: {response.status_code} {response.text}"
                )
            body = response.json_body or []
            if isinstance(body, list):
                all_items.extend(body)
            if first:
                first_page_etag = response.headers.get("etag")
            next_path = _next_link(response.headers.get("link"))
            first = False

        return Page(items=all_items, etag=first_page_etag, not_modified=False)

    # -- writes: the ONLY two write operations this client exposes --------

    def add_labels(
        self, owner: str, repo: str, issue_number: int, labels: Iterable[str]
    ) -> HttpResponseLike:
        """``POST /repos/{owner}/{repo}/issues/{issue_number}/labels`` (§8.2)."""
        self._check_rate_limit()
        response = self._http.request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            json={"labels": list(labels)},
        )
        self.requests_made += 1
        self._record_rate_limit(response)
        if response.status_code >= 400:
            raise GitHubRequestError(f"add_labels failed: {response.status_code} {response.text}")
        return response

    def add_comment(self, owner: str, repo: str, issue_number: int, body: str) -> HttpResponseLike:
        """``POST /repos/{owner}/{repo}/issues/{issue_number}/comments`` (§8.2)."""
        self._check_rate_limit()
        response = self._http.request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        self.requests_made += 1
        self._record_rate_limit(response)
        if response.status_code >= 400:
            raise GitHubRequestError(f"add_comment failed: {response.status_code} {response.text}")
        return response


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
