"""Tests for agents.repo_maint.gh (SPEC_REPO_MAINT.md §3, §8.2, §11)."""

from __future__ import annotations

import ast
import inspect

import httpx
import pytest

from agents.repo_maint import gh
from tests.repo_maint.fakes import gh_client


def _client(handler, **kwargs) -> gh.GitHubClient:
    return gh_client(handler, **kwargs)


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


# -- conditional requests (ETags, via agents_core Http.download) -----------


def _etag_handler(body, etag, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"x-ratelimit-remaining": "4999"})
        return httpx.Response(200, json=body, headers={"etag": etag})

    return handler


def test_get_json_is_conditional_with_a_cache_key(tmp_path):
    seen: list = []
    client = _client(_etag_handler({"full_name": "o/r"}, 'W/"e1"', seen), cache_dir=tmp_path)
    first = client.get_json("/repos/o/r", cache_key="meta")
    second = client.get_json("/repos/o/r", cache_key="meta")

    assert seen == [None, 'W/"e1"']
    assert first.not_modified is False and first.etag == 'W/"e1"'
    assert second.not_modified is True
    assert second.items == [{"full_name": "o/r"}]  # the previous body, from disk
    assert client.not_modified_count == 1
    assert client.requests_made == 2
    assert (tmp_path / "meta.json").is_file() and (tmp_path / "meta.json.meta.json").is_file()


def test_paginate_returns_every_cached_page_on_a_first_page_304(tmp_path):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        seen.append((page, request.headers.get("if-none-match")))
        if page == "2":
            return httpx.Response(200, json=[{"number": 3}])
        if request.headers.get("if-none-match") == 'W/"p1"':
            return httpx.Response(304)
        return httpx.Response(200, json=[{"number": 1}, {"number": 2}], headers={"etag": 'W/"p1"'})

    client = _client(handler, cache_dir=tmp_path)
    first = client.paginate("/repos/o/r/issues", params={"per_page": 2}, cache_key="issues")
    second = client.paginate("/repos/o/r/issues", params={"per_page": 2}, cache_key="issues")

    assert [i["number"] for i in first.items] == [1, 2, 3]
    assert second.not_modified is True
    assert [i["number"] for i in second.items] == [1, 2, 3]
    assert seen == [(None, None), ("2", None), (None, 'W/"p1"')]


def test_paginate_refetches_when_the_saved_pages_belong_to_another_etag(tmp_path):
    seen: list = []
    client = _client(_etag_handler([{"number": 1}], 'W/"e1"', seen), cache_dir=tmp_path)
    client.paginate("/repos/o/r/issues", cache_key="issues")
    (tmp_path / "issues.pages.json").write_text('{"etag": "W/\\"old\\"", "items": []}')

    page = client.paginate("/repos/o/r/issues", cache_key="issues")
    assert page.items == [{"number": 1}]
    assert seen == [None, 'W/"e1"', None]  # the 304 was followed by a forced re-fetch


def test_conditional_404_reads_as_empty(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = _client(handler, cache_dir=tmp_path)
    assert client.get_json("/repos/o/r", cache_key="meta").items == []
    assert client.paginate("/repos/o/r/labels", cache_key="labels").items == []


def test_conditional_reads_raise_on_other_errors(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    with pytest.raises(gh.GitHubRequestError):
        _client(handler, cache_dir=tmp_path).get_json("/repos/o/r", cache_key="meta")


def test_without_a_cache_dir_reads_are_unconditional():
    seen: list = []
    client = _client(_etag_handler([{"number": 1}], 'W/"e1"', seen))
    client.paginate("/repos/o/r/issues", cache_key="issues")
    client.paginate("/repos/o/r/issues", cache_key="issues")
    assert seen == [None, None]


def test_cache_keys_must_be_plain_names(tmp_path):
    client = _client(lambda r: httpx.Response(200, json={}), cache_dir=tmp_path)
    with pytest.raises(ValueError):
        client.get_json("/repos/o/r", cache_key="../escape")


def test_requests_carry_github_headers_and_bypass_the_http_cache():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    _client(handler).get_json("/repos/o/r")
    assert seen["authorization"] == "Bearer test-token"
    assert seen["accept"] == gh.GITHUB_ACCEPT
    assert seen["x-github-api-version"] == gh.GITHUB_API_VERSION


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


def test_get_json_returns_empty_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"message": "Not Found"}, headers={"x-ratelimit-remaining": "4999"}
        )

    client = _client(handler)
    page = client.get_json("/repos/o/r/releases/latest")

    assert page.items == []
    assert page.not_modified is False


def test_get_json_returns_empty_on_409_empty_repository():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "Git Repository is empty."})

    page = _client(handler).get_json("/repos/o/r/commits/main/check-runs")
    assert page.items == []


def test_reads_raise_on_other_error_statuses():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    with pytest.raises(gh.GitHubRequestError):
        _client(handler).get_json("/repos/o/r")
    with pytest.raises(gh.GitHubRequestError):
        _client(handler).paginate("/repos/o/r/issues")


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
        return httpx.Response(
            422, json={"message": "nope"}, headers={"x-ratelimit-remaining": "4999"}
        )

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


def test_default_headers_includes_bearer_token():
    headers = gh.default_headers("abc123")
    assert headers["Authorization"] == "Bearer abc123"
    assert headers["Accept"] == gh.GITHUB_ACCEPT
    assert headers["X-GitHub-Api-Version"] == gh.GITHUB_API_VERSION


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


def test_gh_module_does_not_implement_its_own_networking():
    """gh.py must not implement retries/backoff/caching itself -- that belongs to
    agents_core.http. It only ever calls ``self._http.request(...)`` on the
    injected ``agents_core.http.Http``."""
    source = inspect.getsource(gh)
    assert "from agents_core.http import Http" in source
    assert "import httpx" not in source
    assert "httpx.Client" not in source
    assert "time.sleep" not in source
    assert "urllib" not in source
