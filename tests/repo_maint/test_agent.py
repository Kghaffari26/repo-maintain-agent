"""Tests for agents.repo_maint.agent: registration, and full runs through the real
``agents_core.runner`` (fetch -> transform -> analyze -> publish) against a mocked
GitHub API and a scripted model. Checks the data-branch contract files."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from agents_core import registry, runner

from agents.repo_maint.agent import AGENT, RepoMaintAgent, repos_filter
from agents.repo_maint.schema import RepoMaintOutput
from tests.repo_maint.fakes import FakeAnthropic, mock_http
from tests.repo_maint.test_pipeline import TRIAGE, make_handler

CONFIG = """
[[repo]]
full_name = "you/widgets"
role = "own"
label_map = { bug = "bug", feature = "enhancement" }
"""


@pytest.fixture
def agent(tmp_path, monkeypatch) -> RepoMaintAgent:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.delenv("APPLY_CHANGES", raising=False)
    monkeypatch.setenv("AGENTS_CORE_PUBLISH_DIR", str(tmp_path / "public-data"))
    monkeypatch.setenv("AGENTS_CORE_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "repos.toml").write_text(CONFIG)
    a = RepoMaintAgent()
    a.config_path = tmp_path / "repos.toml"
    a.state_path = tmp_path / "data" / "repo_maint" / "state.json"
    return a


def _run(agent, *, requests=None, llm=None, **kwargs) -> int:
    def track(request: httpx.Request) -> None:
        if requests is not None:
            requests.append(request)

    return runner.run(
        agent, http=mock_http(make_handler(track)), llm_client=llm or FakeAnthropic([]), **kwargs
    )


# -- registration ----------------------------------------------------------------------


def test_registered_under_the_agents_core_entry_point():
    assert registry.discover_agents()["repo_maint"] == "agents.repo_maint.agent:AGENT"
    assert registry.load("repo_maint") is AGENT


def test_manifest_fields_match_the_spec():
    assert AGENT.id == "repo_maint"
    assert AGENT.route == "/repos"
    assert AGENT.expected_interval_hours == 24
    assert AGENT.next_run_hint == "Daily 07:00 PT"
    assert AGENT.output_model is RepoMaintOutput


def test_repos_filter_parses_both_forms():
    assert repos_filter(["--repos", "a/b, c/d"]) == {"a/b", "c/d"}
    assert repos_filter(["--repos=a/b"]) == {"a/b"}
    assert repos_filter(["--other"]) is None


# -- runs ------------------------------------------------------------------------------


def test_dry_run_makes_no_llm_calls_no_writes_and_publishes_nothing(agent, tmp_path):
    requests: list[httpx.Request] = []
    llm = FakeAnthropic([])
    assert _run(agent, requests=requests, llm=llm, dry_run=True) == 0
    assert llm.calls == []
    assert requests and all(r.method == "GET" for r in requests)
    assert not (tmp_path / "public-data").exists()
    assert not agent.state_path.exists()


def test_report_run_publishes_the_data_branch_contract(agent, tmp_path):
    requests: list[httpx.Request] = []
    llm = FakeAnthropic([TRIAGE, TRIAGE])
    assert _run(agent, requests=requests, llm=llm) == 0

    publish = tmp_path / "public-data"
    for name in ("latest.json", "manifest-entry.json", "costs-summary.json", "schema.json"):
        assert (publish / name).is_file(), name
    assert len(list((publish / "history").glob("*.json"))) == 1

    latest = json.loads((publish / "latest.json").read_text())
    output = RepoMaintOutput.model_validate(latest)
    assert output.meta.agent == "repo_maint"
    assert output.meta.github_requests > 0
    assert output.meta.cost_usd > 0
    assert output.mode == "report"
    assert all(a["status"] == "planned" for a in latest["actions"])
    assert "_meta_extra" not in latest

    manifest = json.loads((publish / "manifest-entry.json").read_text())
    assert manifest["id"] == "repo_maint" and manifest["items_count"] == 1
    assert all(r.method == "GET" for r in requests)  # §13: zero writes in report mode
    assert agent.state_path.is_file()
    assert (tmp_path / "data" / "costs.jsonl").is_file()


def test_immediate_second_run_makes_zero_llm_calls(agent):
    assert _run(agent, llm=FakeAnthropic([TRIAGE, TRIAGE])) == 0
    second = FakeAnthropic([])
    assert _run(agent, llm=second) == 0
    assert second.calls == []


def test_apply_flag_alone_never_writes(agent):
    """Gate 1 without gates 2-4 (APPLY_CHANGES unset, allow_apply false): planned only."""
    requests: list[httpx.Request] = []
    assert _run(agent, requests=requests, llm=FakeAnthropic([TRIAGE, TRIAGE]), apply=True) == 0
    assert all(r.method == "GET" for r in requests)


def test_unknown_repos_filter_fails_the_run(agent, tmp_path):
    assert _run(agent, extra_args=["--repos", "you/nope"]) == 1
    manifest = json.loads((Path(tmp_path) / "public-data" / "manifest-entry.json").read_text())
    assert manifest["status"] == "failed"
