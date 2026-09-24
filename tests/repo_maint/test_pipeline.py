"""End-to-end integration test for agents.repo_maint.pipeline (§4, §11).

Runs the full pipeline against a mocked GitHub API (FakeHttpClient /
httpx.MockTransport) for one synthetic repo, with no LLM injected --
exactly the shape of tonight's real report-mode run. Asserts the output
validates against the §6 schema and that report mode makes zero writes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from agents.repo_maint.config import Config, RepoConfig, Settings
from agents.repo_maint.gh import GitHubClient
from agents.repo_maint.pipeline import run
from agents.repo_maint.schema import RepoMaintOutput
from agents.repo_maint.state import State
from tests.repo_maint.fakes import FakeHttpClient

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
            return httpx.Response(200, json=body, headers=headers)
        if path == "/repos/you/widgets/labels":
            labels = [{"name": "bug"}, {"name": "enhancement"}]
            return httpx.Response(200, json=labels, headers=headers)
        if path == "/repos/you/widgets/issues":
            state = request.url.params.get("state")
            if state == "closed":
                return httpx.Response(200, json=[], headers=headers)
            issues = [
                _issue(1, "Crash on load", "2026-09-01T00:00:00Z", body="It crashes."),
                _issue(2, "Add export button", "2026-09-10T00:00:00Z", body="Please add export."),
            ]
            return httpx.Response(200, json=issues, headers=headers)
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


def _run(on_request=None):
    config = Config(settings=Settings(), repo=[REPO])
    state = State()

    def build_client(repo: RepoConfig) -> GitHubClient:
        return GitHubClient(http=FakeHttpClient(make_handler(on_request), base_url=BASE_URL))

    output = run(
        config,
        state,
        build_client,
        apply_flag=False,
        apply_changes_env=None,
        now=NOW,
        run_id="test-run",
    )
    return output, state


def test_pipeline_runs_end_to_end_and_output_validates():
    output, _state = _run()

    # validates against the real §6 schema
    RepoMaintOutput.model_validate(output.model_dump(mode="json"))

    assert output.mode == "report"
    assert len(output.repos) == 1
    repo_entry = output.repos[0]
    assert repo_entry.full_name == "you/widgets"
    assert repo_entry.counts.open_issues == 2
    # neither issue has a triaged label or maintainer comment
    assert repo_entry.counts.untriaged == 2
    # PR last updated 2026-08-01, more than stale_days before NOW
    assert repo_entry.counts.stale_prs == 1
    assert repo_entry.health.score <= 100

    # no LLM injected -> nothing classified yet, so no triage items and no actions
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


def test_pipeline_populates_state_changelog_cache():
    _output, state = _run()
    repo_state = state.repos["you/widgets"]
    assert repo_state.changelog_cache is not None
    assert "markdown" in repo_state.changelog_cache
