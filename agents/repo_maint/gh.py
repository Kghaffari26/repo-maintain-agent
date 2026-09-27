"""Thin GitHub REST client for the repo maintenance agent (SPEC_REPO_MAINT.md §3, §8.2).

This module has **no networking implementation of its own**: every request goes
through an injected ``agents_core.http.Http`` (retries, backoff, secret-safe
logging, per-host caps). Conditional reads (ETags) use agents-core's
``Http.download`` helper, which keeps the last body and its ETag on disk under the
client's ``cache_dir`` and answers a 304 from there. Every other read passes
``ttl_seconds=0`` so agents-core's 6-hour dev cache never serves stale issue data.

The client exposes **exactly two** write operations (``add_labels`` and
``add_comment``). There are no other write endpoints here on purpose, so the
rest of the agent cannot express any write the spec doesn't allow -- see
``test_gh_client.py::test_only_two_write_methods_exist``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
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

#: Conditional-read cache keys become file names under ``cache_dir``.
_CACHE_KEY = re.compile(r"^[a-z0-9_]+$")


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
    ) -> tuple[Any, str | None, bool] | None:
        """A conditional GET through ``Http.download``. Returns ``(body, etag,
        not_modified)``, or None for a 404/409 ("nothing here")."""
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
        # Http.download doesn't expose response headers, so rate-limit tracking comes
        # from the unconditional reads (304s don't count against GitHub's limit anyway).
        self._record({})
        if not result.modified:
            self.not_modified_count += 1
        try:
            body = json.loads(dest.read_text())
        except (OSError, json.JSONDecodeError):
            if force:
                raise GitHubRequestError(f"GET {path}: unreadable response body") from None
            return self._download(path, cache_key, params, force=True)
        return body, result.etag, not result.modified

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
        body, etag, not_modified = got
        return Page(items=_as_items(body), etag=etag, not_modified=not_modified)

    def paginate(
        self, path: str, *, params: dict[str, Any] | None = None, cache_key: str | None = None
    ) -> Page:
        """Every page of a list endpoint.

        With a ``cache_key``, the first page is a conditional GET: a 304 there means
        the whole collection is unchanged (these endpoints are polled sorted by
        ``updated``), and all pages are read back from ``<key>.pages.json``, which
        records the first page's ETag it belongs to. Without one, ``Link: rel="next"``
        headers are followed.
        """
        page_params: dict[str, Any] = dict(params or {})
        page_params.setdefault("per_page", 100)
        if cache_key is None or self.cache_dir is None:
            return self._paginate_links(path, page_params)

        pages_path = self._cache_path(cache_key).with_suffix(".pages.json")
        got = self._download(path, cache_key, page_params)
        if got is None:
            return Page(items=[], etag=None, not_modified=False)
        first, etag, not_modified = got
        if not_modified:
            saved = _read_pages(pages_path)
            if saved is not None and saved.get("etag") == etag:
                return Page(items=saved["items"], etag=etag, not_modified=True)
            # The pages file is missing or belongs to another ETag: fetch it all again.
            got = self._download(path, cache_key, page_params, force=True)
            if got is None:
                return Page(items=[], etag=None, not_modified=False)
            first, etag, _ = got

        items = list(first) if isinstance(first, list) else []
        per_page = int(page_params["per_page"])
        number = 1
        last_count = len(items)
        while last_count >= per_page:
            number += 1
            result = self._get(path, params={**page_params, "page": number})
            if result.status in EMPTY_STATUSES or not isinstance(result.body, list):
                break
            items.extend(result.body)
            last_count = len(result.body)
        pages_path.write_text(json.dumps({"etag": etag, "items": items}))
        return Page(items=items, etag=etag, not_modified=False)

    def _paginate_links(self, path: str, page_params: dict[str, Any]) -> Page:
        all_items: list[dict[str, Any]] = []
        next_path: str | None = path
        first_page_etag: str | None = None
        first = True
        while next_path:
            result = self._get(next_path, params=page_params if first else None)
            if result.status in EMPTY_STATUSES:
                break
            if isinstance(result.body, list):
                all_items.extend(result.body)
            if first:
                first_page_etag = result.headers.get("etag")
            next_path = _next_link(result.headers.get("link"))
            first = False
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


def _read_pages(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


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
