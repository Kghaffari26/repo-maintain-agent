"""End-to-end integration test for agents.repo_maint.pipeline (§4, §11).

Runs the full pipeline against a mocked GitHub API (agents_core ``Http`` over
``httpx.MockTransport``) for one synthetic repo, with and without a scripted
model behind agents_core's ``LLM``. Asserts the output validates against the §6
schema, that report mode makes zero writes, and that ETags/caches carry over.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from agents_core.schema import ModelUsage, RunMeta

from agents.repo_maint import changelog as changelog_mod
from agents.repo_maint import triage as triage_mod
from agents.repo_maint.config import Config, RepoConfig, Settings
from agents.repo_maint.gh import GitHubClient
from agents.repo_maint.pipeline import run
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


def _run(on_request=None, state=None, llm=None, config=None, handler=None):
    config = config or _config()
    state = state if state is not None else State()

    def build_client(repo: RepoConfig) -> GitHubClient:
        return gh_client(handler or make_handler(on_request))

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


def _validate(body) -> RepoMaintOutput:
    """What agents_core.runner does: add its own meta, validate against the model."""
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
    return RepoMaintOutput.model_validate({**body, "meta": meta.model_dump(mode="json")})


def test_pipeline_runs_end_to_end_and_output_validates():
    result, _state = _run()
    output = _validate(result.body)

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
    assert cache["narrative_source"] == "deterministic"
    assert cache["markdown"] == "## Unreleased\n"


def test_pipeline_persists_etags_and_their_bodies():
    _result, state = _run()
    repo_state = state.repos["you/widgets"]
    assert repo_state.etags.get("meta") == 'W/"meta-etag"'
    assert repo_state.etags.get("issues_open") == 'W/"issues-etag"'
    assert set(repo_state.etag_bodies) == {v for v in repo_state.etags.values() if v}


def test_second_run_gets_304s_and_still_sees_the_same_issues():
    _result, state = _run()

    seen_if_none_match = {}

    def track(request: httpx.Request) -> None:
        is_meta = request.url.path == "/repos/you/widgets"
        is_open_issues = (
            request.url.path == "/repos/you/widgets/issues"
            and request.url.params.get("state") == "open"
        )
        if is_meta or is_open_issues:
            seen_if_none_match[request.url.path] = request.headers.get("if-none-match")

    result, _state = _run(on_request=track, state=state)
    output = _validate(result.body)

    assert seen_if_none_match["/repos/you/widgets"] == 'W/"meta-etag"'
    assert seen_if_none_match["/repos/you/widgets/issues"] == 'W/"issues-etag"'
    assert output.meta.github_304s >= 2
    assert output.repos[0].counts.open_issues == 2  # served from the 304's cached body


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
    output = _validate(result.body)

    assert len(client.calls) == 2  # two untriaged issues, no merged PRs -> no changelog call
    assert [t.number for t in output.repos[0].triage] == [1, 2]
    assert all(t.cached is False for t in output.repos[0].triage)
    assert output.actions and all(a.status == "planned" for a in output.actions)
    assert all("--apply" in (a.reason or "") for a in output.actions)
    assert state.repos["you/widgets"].changelog_cache is not None  # empty set: cacheable

    llm2, client2 = fake_llm([], tmp_path)
    result2, _state = _run(llm=llm2, state=state)
    output2 = _validate(result2.body)
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
    output = _validate(result.body)

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
