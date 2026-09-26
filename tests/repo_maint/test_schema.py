"""Tests for agents.repo_maint.schema (§6, §11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agents_core.schema import AgentOutput, ModelUsage, RunMeta
from pydantic import ValidationError

from agents.repo_maint.schema import (
    META_EXTRA_KEY,
    ActionEntry,
    Activity12w,
    ChangelogBlock,
    Counts,
    HealthBlock,
    KeyStat,
    RepoEntry,
    RepoMaintMeta,
    RepoMaintOutput,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def make_output() -> RepoMaintOutput:
    meta = RepoMaintMeta(
        agent="repo_maint",
        schema_version="1.0.0",
        run_id="run-1",
        started_at=NOW,
        finished_at=NOW,
        status="ok",
        data_changed=True,
        cost_usd=0.0,
        model_usage=ModelUsage(),
        sources=[],
        github_requests=42,
        github_304s=10,
    )
    repo = RepoEntry(
        full_name="you/repo",
        url="https://github.com/you/repo",
        role="own",
        allow_apply=False,
        partial=False,
        health=HealthBlock(score=91, grade="A", breakdown=[]),
        counts=Counts(
            open_issues=3,
            untriaged=1,
            untriaged_over_7d=0,
            open_prs=1,
            stale_prs=0,
            no_response_count=0,
        ),
        median_first_response_hours=12.5,
        ci_default_branch="success",
        days_since_release=10,
        activity_12w=Activity12w(weeks=["2026-W39"], opened=[1], closed=[0]),
        triage=[],
        stale_prs=[],
        changelog=ChangelogBlock(
            base_ref="v1.0.0",
            base_date="2026-08-01",
            source="pull_requests",
            item_count=0,
            suggested_version=None,
            markdown="## [Unreleased]\n",
            narrative_source="deterministic",
            model=None,
            generated_at=NOW,
            cached=False,
        ),
    )
    return RepoMaintOutput(
        meta=meta,
        headline="1 repo watched.",
        key_stats=[
            KeyStat(label="Untriaged issues", value=1, format="count", good_direction="down")
        ],
        mode="report",
        repos=[repo],
        actions=[
            ActionEntry(
                repo="you/repo",
                type="comment",
                target=1,
                detail="triage comment",
                status="planned",
                reason="report mode",
            )
        ],
    )


def test_fixture_output_validates():
    output = make_output()
    dumped = output.model_dump(mode="json")
    # round-trips through validation again (as the real publish path would)
    RepoMaintOutput.model_validate(dumped)


def test_meta_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RepoMaintMeta(
            agent="repo_maint",
            schema_version="1.0.0",
            run_id="run-1",
            started_at=NOW,
            finished_at=NOW,
            status="ok",
            data_changed=True,
            cost_usd=0.0,
            model_usage=ModelUsage(),
            sources=[],
            github_requests=1,
            github_304s=1,
            made_up_field="nope",
        )


def test_health_score_bounds_enforced():
    with pytest.raises(ValidationError):
        HealthBlock(score=150, grade="A", breakdown=[])


def test_mode_only_accepts_report_or_apply():
    output = make_output()
    dumped = output.model_dump(mode="json")
    dumped["mode"] = "delete_everything"
    with pytest.raises(ValidationError):
        RepoMaintOutput.model_validate(dumped)


def test_key_stats_capped_at_four():
    output = make_output()
    dumped = output.model_dump(mode="json")
    dumped["key_stats"] = [
        {"label": f"stat {i}", "value": i, "format": "count", "good_direction": "neutral"}
        for i in range(5)
    ]
    with pytest.raises(ValidationError):
        RepoMaintOutput.model_validate(dumped)


def test_json_schema_export_is_stable_and_has_expected_top_level_keys():
    schema = RepoMaintOutput.model_json_schema()
    expected_keys = {"meta", "headline", "key_stats", "mode", "repos", "actions"}
    assert set(schema["properties"]) == expected_keys
    # every field the JSON contract promises is marked required (no optional drift)
    assert set(schema["required"]) == expected_keys


# -- agents_core integration ------------------------------------------------------------


def test_output_builds_on_agents_core_models():
    assert issubclass(RepoMaintOutput, AgentOutput)
    assert issubclass(RepoMaintMeta, RunMeta)


def test_meta_extra_is_folded_into_meta_like_agents_core_runner_needs():
    """agents_core.runner validates {**body, "meta": <its own RunMeta>}; the §6 meta
    extensions ride in the body under META_EXTRA_KEY and must land in meta."""
    dumped = make_output().model_dump(mode="json")
    meta = dumped.pop("meta")
    runner_meta = {k: v for k, v in meta.items() if k not in {"github_requests", "github_304s"}}
    body = {**dumped, META_EXTRA_KEY: {"github_requests": 7, "github_304s": 3}}

    output = RepoMaintOutput.model_validate({**body, "meta": runner_meta})

    assert output.meta.github_requests == 7
    assert output.meta.github_304s == 3
    assert META_EXTRA_KEY not in output.model_dump(mode="json")


def test_meta_extensions_are_required():
    dumped = make_output().model_dump(mode="json")
    del dumped["meta"]["github_304s"]
    with pytest.raises(ValidationError):
        RepoMaintOutput.model_validate(dumped)
