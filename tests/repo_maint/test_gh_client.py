"""Tests for agents.repo_maint.gh (SPEC_REPO_MAINT.md §3, §8.2, §11)."""

from __future__ import annotations

import ast
import inspect

import httpx
import pytest

from agents.repo_maint import gh
from core.http import HttpClient


def _client(handler) -> gh.GitHubClient:
    transport = httpx.MockTransport(handler)
    http = HttpClient(base_url=gh.GITHUB_API_BASE, transport=transport)
    return gh.GitHubClient(token="fake-token", http=http)


# -- pagination -----------------------------------------------------------


def test_paginate_follows_link_header():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(
                200,
                json=[{"number": 3}],
                headers={"x-ratelimit-remaining": "4998"},
            )
        next_url = f"{gh.GITHUB_API_BASE}/repos/o/r/issues?per_page=100&page=2"
        return httpx.Response(
            200,
            json=[{"number": 1}, {"number": 2}],
            headers={
                "link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"',
                "etag": 'W/"first-page"',
                "x-ratelimit-remaining": "4999",
            },
        )

    client = _client(handler)
    page = client.paginate("/repos/o/r/issues")

    assert [item["number"] for item in page.items] == [1, 2, 3]
    assert page.not_modified is False
    assert page.etag == 'W/"first-page"'
    assert len(calls) == 2
    assert client.requests_made == 2


def test_paginate_stops_when_no_next_link():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": 1}], headers={"x-ratelimit-remaining": "4999"})

    client = _client(handler)
    page = client.paginate("/repos/o/r/issues")

    assert [item["number"] for item in page.items] == [1]


# -- conditional requests (ETags) -----------------------------------------


def test_get_json_returns_not_modified_on_304():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("if-none-match") == 'W/"cached"'
        return httpx.Response(304, headers={"x-ratelimit-remaining": "4999"})

    client = _client(handler)
    page = client.get_json("/repos/o/r", etag='W/"cached"')

    assert page.not_modified is True
    assert page.items == []
    assert client.not_modified_count == 1


def test_paginate_returns_not_modified_on_304_first_page():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, headers={"x-ratelimit-remaining": "4999"})

    client = _client(handler)
    page = client.paginate("/repos/o/r/issues", etag='W/"cached"')

    assert page.not_modified is True
    assert page.items == []


def test_get_json_returns_fresh_etag_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"full_name": "o/r"},
            headers={"etag": 'W/"fresh"', "x-ratelimit-remaining": "4999"},
        )

    client = _client(handler)
    page = client.get_json("/repos/o/r")

    assert page.not_modified is False
    assert page.etag == 'W/"fresh"'
    assert page.items == [{"full_name": "o/r"}]


# -- rate-limit guard -------------------------------------------------------


def test_rate_limit_guard_stops_before_floor():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers={"x-ratelimit-remaining": "99"})

    client = _client(handler)
    client.get_json("/repos/o/r/issues")  # records remaining=99, below the floor
    assert client.rate_limit_remaining == 99

    with pytest.raises(gh.RateLimitLow):
        client.get_json("/repos/o/r/issues")


def test_rate_limit_guard_allows_requests_above_floor():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers={"x-ratelimit-remaining": "500"})

    client = _client(handler)
    client.get_json("/repos/o/r/issues")
    client.get_json("/repos/o/r/issues")  # should not raise
    assert client.requests_made == 2


# -- writes -----------------------------------------------------------------


def test_add_labels_posts_expected_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json=[], headers={"x-ratelimit-remaining": "4999"})

    client = _client(handler)
    client.add_labels("o", "r", 42, ["bug", "area: site"])

    assert captured["method"] == "POST"
    assert captured["url"] == f"{gh.GITHUB_API_BASE}/repos/o/r/issues/42/labels"
    assert b'"bug"' in captured["body"] and b'"area: site"' in captured["body"]


def test_add_comment_posts_expected_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(201, json={"id": 1}, headers={"x-ratelimit-remaining": "4999"})

    client = _client(handler)
    client.add_comment("o", "r", 42, "hello")

    assert captured["method"] == "POST"
    assert captured["url"] == f"{gh.GITHUB_API_BASE}/repos/o/r/issues/42/comments"
    assert b"hello" in captured["body"]


def test_writes_raise_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "nope"}, headers={"x-ratelimit-remaining": "4999"})

    client = _client(handler)
    with pytest.raises(gh.GitHubRequestError):
        client.add_labels("o", "r", 42, ["bug"])
    with pytest.raises(gh.GitHubRequestError):
        client.add_comment("o", "r", 42, "hello")


# -- token resolution ---------------------------------------------------------


def test_resolve_token_reads_default_env_var(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "abc123")
    assert gh.resolve_token("default") == "abc123"


def test_resolve_token_reads_repo_maint_env_var(monkeypatch):
    monkeypatch.setenv("REPO_MAINT_TOKEN", "def456")
    assert gh.resolve_token("repo_maint") == "def456"


def test_resolve_token_missing_raises(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(gh.GitHubTokenMissing):
        gh.resolve_token("default")


def test_resolve_token_unknown_name_raises():
    with pytest.raises(ValueError):
        gh.resolve_token("something_else")


# -- introspection: no write methods beyond the two allowed (§8.2, §11) -----


def test_only_two_write_methods_exist():
    """Statically verify no code path issues a non-GET request outside
    ``add_labels``/``add_comment``, so the client can't express any other write."""
    write_verbs = {"POST", "PUT", "PATCH", "DELETE"}
    source = inspect.getsource(gh)
    tree = ast.parse(source)

    functions_with_writes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr != "request" or not call.args:
                continue
            first_arg = call.args[0]
            if isinstance(first_arg, ast.Constant) and first_arg.value in write_verbs:
                functions_with_writes.add(node.name)

    assert functions_with_writes == {"add_labels", "add_comment"}


def test_github_client_has_no_other_public_write_looking_methods():
    """Belt-and-braces: the only public methods with write-suggestive names
    are the two allowed writes."""
    write_ish_prefixes = ("add", "create", "post", "update", "delete", "remove", "close", "merge")
    public_methods = [
        name
        for name in dir(gh.GitHubClient)
        if not name.startswith("_") and callable(getattr(gh.GitHubClient, name))
    ]
    write_ish = {
        name
        for name in public_methods
        if name.startswith(write_ish_prefixes) and name != "close"
    }
    assert write_ish == {"add_labels", "add_comment"}
