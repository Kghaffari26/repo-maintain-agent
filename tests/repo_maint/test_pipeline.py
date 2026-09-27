"""End-to-end integration test for agents.repo_maint.pipeline (§4, §11).

Runs the full pipeline against a mocked GitHub API (agents_core ``Http`` over
``httpx.MockTransport``) for one synthetic repo, with and without a scripted
model behind agents_core's ``LLM``. Asserts the output validates against the §6
schema, that report mode makes zero writes, and that ETags/caches carry over.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from agents_core.llm import LLMError
from agents_core.schema import ModelUsage, RunMeta

from agents.repo_maint import changelog as changelog_mod
from agents.repo_maint import triage as triage_mod
from agents.repo_maint.config import Config, RepoConfig, Settings
from agents.repo_maint.gh import GitHubClient
from agents.repo_maint.pipeline import previous_key_stats, run
from agents.repo_maint.schema import RepoMaintOutput
from agents.repo_maint.state import State
from tests.repo_maint.fakes import fake_llm, gh_client

NOW = datetime(2026, 9, 24, tzinfo=UTC)
BASE_URL = "https://api.github.com"

REPO = RepoConfig(
    full_name="you/widgets",
    role="own",
    allow_apply=False,
    token="default",
    label_map={"bug": "bug", "feature": "enhancement"},
)


def _issue(number, title, created_at, labels=None, body="", pull_request=None):
    item = {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in (labels or [])],
        "created_at": created_at,
        "closed_at": None,
        "state": "open",
        "user": {"login": "reporter", "type": "User"},
    }
    if pull_request:
        item["pull_request"] = {}
    return item


def _pr(number, title, created_at, updated_at, draft=False):
    return {
        "number": number,
        "title": title,
        "body": "",
        "created_at": created_at,
        "updated_at": updated_at,
        "draft": draft,
        "user": {"login": "author"},
        "head": {"sha": f"sha{number}"},
        "base": {"ref": "main"},
        "requested_reviewers": [],
        "merged_at": None,
    }


def make_handler(on_request=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if on_request:
            on_request(request)
        path = request.url.path
        headers = {"x-ratelimit-remaining": "4999"}

        if path == "/repos/you/widgets":
            body = {"default_branch": "main", "full_name": "you/widgets"}
            meta_headers = {**headers, "etag": 'W/"meta-etag"'}
            return httpx.Response(200, json=body, headers=meta_headers)
        if path == "/repos/you/widgets/labels":
            labels = [{"name": "bug"}, {"name": "enhancement"}]
            if request.headers.get("if-none-match") == 'W/"labels-etag"':
                return httpx.Response(304, headers=headers)
            return httpx.Response(200, json=labels, headers={**headers, "etag": 'W/"labels-etag"'})
        if path == "/repos/you/widgets/issues":
            state = request.url.params.get("state")
            if state == "closed":
                return httpx.Response(200, json=[], headers=headers)
            issues = [
                _issue(1, "Crash on load", "2026-09-01T00:00:00Z", body="It crashes."),
                _issue(2, "Add export button", "2026-09-10T00:00:00Z", body="Please add export."),
            ]
            if state == "open" and request.headers.get("if-none-match") == 'W/"issues-etag"':
                return httpx.Response(304, headers=headers)
            issues_headers = {**headers, "etag": 'W/"issues-etag"'} if state == "open" else headers
            return httpx.Response(200, json=issues, headers=issues_headers)
        if path in ("/repos/you/widgets/issues/1/comments", "/repos/you/widgets/issues/2/comments"):
            return httpx.Response(200, json=[], headers=headers)
        if path == "/repos/you/widgets/pulls":
            state = request.url.params.get("state")
            if state == "closed":
                return httpx.Response(200, json=[], headers=headers)
            prs = [_pr(10, "Fix typo", "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z")]
            return httpx.Response(200, json=prs, headers=headers)
        if path == "/repos/you/widgets/pulls/10/reviews":
            return httpx.Response(200, json=[], headers=headers)
        if path.startswith("/repos/you/widgets/commits/") and path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []}, headers=headers)
        if path == "/repos/you/widgets/releases/latest":
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
        if path == "/repos/you/widgets/tags":
            return httpx.Response(200, json=[], headers=headers)
        if path == "/repos/you/widgets/community/profile":
            profile = {"readme": {"name": "README.md"}, "license": None, "contributing": None}
            return httpx.Response(200, json={"files": profile}, headers=headers)
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return handler


def _config(*repos: RepoConfig) -> Config:
    return Config(settings=Settings(), repo=list(repos or [REPO]))


def _run(on_request=None, state=None, llm=None, config=None, handler=None, cache_dir=None):
    config = config or _config()
    state = state if state is not None else State()

    def build_client(repo: RepoConfig) -> GitHubClient:
        return gh_client(handler or make_handler(on_request), cache_dir=cache_dir)

    kwargs = {}
    if llm is not None:
        kwargs = {
            "classify_factory": lambda repo, desc, labels: triage_mod.make_classify_fn(
                llm, repo, desc, labels
            ),
            "draft_fn": changelog_mod.make_draft_fn(llm),
            "draft_model": "claude-sonnet-5",
        }
    result = run(
        config,
        state,
        build_client,
        apply_flag=False,
        apply_changes_env=None,
        now=NOW,
        **kwargs,
    )
    return result, state


def _validate(result) -> RepoMaintOutput:
    """What agents_core.runner does: add its own meta plus the agent's meta_fields,
    validate against the model."""
    meta = RunMeta(
        agent="repo_maint",
        schema_version="1.0.0",
        run_id="test-run",
        started_at=NOW,
        finished_at=NOW,
        status="ok",
        data_changed=True,
        cost_usd=0.0,
        model_usage=ModelUsage(),
        sources=[],
    )
    meta_dict = {**meta.model_dump(mode="json"), **result.meta_fields}
    return RepoMaintOutput.model_validate({**result.body, "meta": meta_dict})


def test_pipeline_runs_end_to_end_and_output_validates():
    result, _state = _run()
    output = _validate(result)

    assert output.mode == "report"
    assert output.meta.github_requests > 0
    assert len(output.repos) == 1
    repo_entry = output.repos[0]
    assert repo_entry.full_name == "you/widgets"
    assert repo_entry.counts.open_issues == 2
    # neither issue has a triaged label or maintainer comment
    assert repo_entry.counts.untriaged == 2
    # PR last updated 2026-08-01, more than stale_days before NOW
    assert repo_entry.counts.stale_prs == 1
    assert repo_entry.health.score <= 100

    # no model (a dry run's shape) -> nothing classified, so no triage items and no actions
    assert repo_entry.triage == []
    assert output.actions == []


def test_pipeline_makes_zero_writes_in_report_mode():
    """§13 acceptance criterion: a report-mode run makes zero write requests."""
    write_calls = []

    def track(request: httpx.Request) -> None:
        if request.method != "GET":
            write_calls.append((request.method, str(request.url)))

    _run(on_request=track)
    assert write_calls == []


def test_an_empty_changelog_needs_no_model_and_is_cached():
    _result, state = _run()
    cache = state.repos["you/widgets"].changelog_cache
    assert cache["narrative_source"] == "template"
    assert cache["markdown"] == "## Unreleased\n"


def test_conditional_reads_are_cached_on_disk(tmp_path):
    _run(cache_dir=tmp_path)
    assert (tmp_path / "meta.json").is_file()
    assert (tmp_path / "issues_open.json.meta.json").is_file()
    assert (tmp_path / "issues_open.pages.json").is_file()


def test_second_run_gets_304s_and_still_sees_the_same_issues(tmp_path):
    _result, state = _run(cache_dir=tmp_path)

    seen_if_none_match = {}

    def track(request: httpx.Request) -> None:
        is_meta = request.url.path == "/repos/you/widgets"
        is_open_issues = (
            request.url.path == "/repos/you/widgets/issues"
            and request.url.params.get("state") == "open"
        )
        if is_meta or is_open_issues:
            seen_if_none_match[request.url.path] = request.headers.get("if-none-match")

    result, _state = _run(on_request=track, state=state, cache_dir=tmp_path)
    output = _validate(result)

    assert seen_if_none_match["/repos/you/widgets"] == 'W/"meta-etag"'
    assert seen_if_none_match["/repos/you/widgets/issues"] == 'W/"issues-etag"'
    assert output.meta.github_304s >= 2
    assert output.repos[0].counts.open_issues == 2  # served from the 304's cached body


def test_warnings_name_unreachable_repos_and_untriaged_leftovers(tmp_path):
    broken = RepoConfig(full_name="you/gone", role="own")
    base_handler = make_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/you/gone"):
            return httpx.Response(404, json={"message": "Not Found"})
        return base_handler(request)

    llm, _client = fake_llm([TRIAGE, LLMError("boom")], tmp_path)
    result, _state = _run(config=_config(broken, REPO), handler=handler, llm=llm)
    assert any(w.startswith("you/gone: skipped this run") for w in result.warnings)
    assert any("1 of 2 untriaged issues weren't triaged" in w for w in result.warnings)


def test_key_stats_carry_deltas_in_a_standard_format():
    result, _state = _run()
    first = {k["label"]: k for k in result.body["key_stats"]}
    assert all(k["delta"] is None for k in first.values())
    assert {k["delta_format"] for k in first.values()} == {"count_signed"}

    previous = previous_key_stats({"key_stats": [{"label": "Untriaged issues", "value": 5}]})
    result = run(
        _config(),
        State(),
        lambda repo: gh_client(make_handler()),
        now=NOW,
        previous_stats=previous,
    )
    stats = {k["label"]: k for k in result.body["key_stats"]}
    assert stats["Untriaged issues"]["delta"] == -3.0  # 2 now vs 5 before
    assert stats["Stale PRs"]["delta"] is None
    assert previous_key_stats(None) is None


TRIAGE = {
    "classification": "bug",
    "priority": "p2",
    "confidence": "high",
    "suggested_labels": ["bug"],
    "missing_info": ["steps to reproduce"],
    "summary": "App crashes on load.",
    "duplicates": [],
    "first_response": "Thanks for the report! Could you share steps to reproduce?",
}


def test_llm_run_triages_plans_actions_and_second_run_is_fully_cached(tmp_path):
    llm, client = fake_llm([TRIAGE, {**TRIAGE, "classification": "feature"}], tmp_path)
    result, state = _run(llm=llm)
    output = _validate(result)

    assert len(client.calls) == 2  # two untriaged issues, no merged PRs -> no changelog call
    assert [t.number for t in output.repos[0].triage] == [1, 2]
    assert all(t.cached is False for t in output.repos[0].triage)
    assert output.actions and all(a.status == "planned" for a in output.actions)
    assert all("--apply" in (a.reason or "") for a in output.actions)
    assert state.repos["you/widgets"].changelog_cache is not None  # empty set: cacheable

    llm2, client2 = fake_llm([], tmp_path)
    result2, _state = _run(llm=llm2, state=state)
    output2 = _validate(result2)
    assert client2.calls == []  # §13: an immediate second run makes zero LLM calls
    assert all(t.cached for t in output2.repos[0].triage)
    assert output2.repos[0].changelog.cached is True


def test_a_failing_repo_is_skipped_not_fatal():
    broken = RepoConfig(full_name="you/gone", role="own")
    base_handler = make_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/you/gone"):
            return httpx.Response(404, json={"message": "Not Found"})
        return base_handler(request)

    result, _state = _run(config=_config(broken, REPO), handler=handler)
    output = _validate(result)

    assert [r.full_name for r in output.repos] == ["you/widgets"]
    assert "you/gone" in result.failed
    assert "(1 unreachable)" in output.headline


def test_the_run_fails_when_every_repo_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    try:
        _run(handler=handler)
    except RuntimeError as e:
        assert "every configured repo failed" in str(e)
    else:
        raise AssertionError("expected the run to fail")
