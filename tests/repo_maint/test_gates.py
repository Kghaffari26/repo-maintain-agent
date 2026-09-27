"""Tests for agents.repo_maint.config: repos.toml parsing and write gates (§8.1, §9, §11)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.repo_maint.config import (
    RepoConfig,
    approve_fix_pr,
    check_write_gates,
    load_config,
)


def make_repo(**overrides) -> RepoConfig:
    defaults = dict(full_name="you/repo", role="own", allow_apply=False, token="default")
    defaults.update(overrides)
    return RepoConfig(**defaults)


# -- config parsing -----------------------------------------------------------


def test_load_config_parses_example_file():
    config = load_config(Path(__file__).parents[2] / "config" / "repos.toml")
    assert config.settings.stale_days == 14
    assert config.health.penalties.ci_failure == 20
    assert len(config.repo) >= 1
    assert config.repo[0].role == "own"


def test_load_config_applies_setting_defaults(tmp_path: Path):
    toml = tmp_path / "repos.toml"
    toml.write_text(
        textwrap.dedent(
            """
            [[repo]]
            full_name = "you/repo"
            role = "own"
            """
        )
    )
    config = load_config(toml)
    assert config.settings.stale_days == 14
    assert config.settings.dup_threshold == 0.45
    assert config.repo[0].allow_apply is False
    assert config.repo[0].token == "default"


def test_config_rejects_duplicate_repo_entries(tmp_path: Path):
    toml = tmp_path / "repos.toml"
    toml.write_text(
        textwrap.dedent(
            """
            [[repo]]
            full_name = "you/repo"
            role = "own"

            [[repo]]
            full_name = "you/repo"
            role = "sandbox"
            """
        )
    )
    with pytest.raises(ValidationError):
        load_config(toml)


def test_repo_config_triaged_labels_union():
    repo = make_repo(
        label_map={"bug": "bug", "feature": "enhancement"},
        priority_labels={"p0": "priority: critical"},
        extra_triaged_labels=["triaged"],
    )
    assert repo.triaged_labels == {"bug", "enhancement", "priority: critical", "triaged"}


def test_repo_config_owner_and_repo_properties():
    repo = make_repo(full_name="acme/widgets")
    assert repo.owner == "acme"
    assert repo.repo == "widgets"


# -- public_demo + allow_apply must fail validation (§8.1 gate 4, §13) -------


def test_public_demo_with_allow_apply_fails_validation():
    with pytest.raises(ValidationError):
        make_repo(role="public_demo", allow_apply=True)


def test_public_demo_without_allow_apply_is_valid():
    repo = make_repo(role="public_demo", allow_apply=False)
    assert repo.role == "public_demo"


def test_config_file_with_public_demo_allow_apply_fails_validation(tmp_path: Path):
    toml = tmp_path / "repos.toml"
    toml.write_text(
        textwrap.dedent(
            """
            [[repo]]
            full_name = "some-org/popular-project"
            role = "public_demo"
            allow_apply = true
            """
        )
    )
    with pytest.raises(ValidationError):
        load_config(toml)


# -- write gates (§8.1): every combination -----------------------------------

ROLES_AND_ALLOWED_APPLY = {
    "own": (True, False),
    "sandbox": (True, False),
    "public_demo": (False,),  # allow_apply=True is unconstructable for this role
}
APPLY_FLAGS = (True, False)
APPLY_CHANGES_ENVS = ("true", "false", None)
TOKEN_AVAILABLE = (True, False)

GATE_COMBINATIONS = [
    (role, allow_apply, apply_flag, apply_changes_env, token_available)
    for role, allow_apply_options in ROLES_AND_ALLOWED_APPLY.items()
    for allow_apply in allow_apply_options
    for apply_flag in APPLY_FLAGS
    for apply_changes_env in APPLY_CHANGES_ENVS
    for token_available in TOKEN_AVAILABLE
]


@pytest.mark.parametrize(
    "role,allow_apply,apply_flag,apply_changes_env,token_available", GATE_COMBINATIONS
)
def test_write_gates_every_combination(
    role, allow_apply, apply_flag, apply_changes_env, token_available
):
    repo = make_repo(role=role, allow_apply=allow_apply)
    result = check_write_gates(
        repo,
        apply_flag=apply_flag,
        apply_changes_env=apply_changes_env,
        token_available=token_available,
    )

    expected = (
        apply_flag
        and apply_changes_env == "true"
        and allow_apply
        and role in ("own", "sandbox")
        and token_available
    )
    assert result.passed == expected
    if not expected:
        assert result.reasons
    else:
        assert result.reasons == []


def test_write_gates_all_pass_for_sandbox():
    repo = make_repo(role="sandbox", allow_apply=True)
    result = check_write_gates(
        repo, apply_flag=True, apply_changes_env="true", token_available=True
    )
    assert result.passed is True
    assert result.reasons == []


def test_write_gates_report_every_failure_reason():
    repo = make_repo(role="public_demo", allow_apply=False)
    result = check_write_gates(
        repo, apply_flag=False, apply_changes_env="false", token_available=False
    )
    assert result.passed is False
    # all 5 gates should have failed and been named
    assert len(result.reasons) == 5


def test_write_gates_reject_own_role_without_allow_apply():
    # A repo you own but haven't opted into apply mode for -- the common case.
    repo = make_repo(role="own", allow_apply=False)
    result = check_write_gates(
        repo, apply_flag=True, apply_changes_env="true", token_available=True
    )
    assert result.passed is False
    assert any("allow_apply" in reason for reason in result.reasons)


# -- the fix-PR gate (§6.1) ----------------------------------------------------------


def test_allow_fix_prs_is_sandbox_only_and_fails_to_load_otherwise():
    for role in ("own", "public_demo"):
        with pytest.raises(ValidationError, match="allow_fix_prs"):
            RepoConfig(full_name="o/r", role=role, allow_fix_prs=True)
    assert RepoConfig(full_name="o/r", role="sandbox", allow_fix_prs=True).allow_fix_prs


def _fix_repo(**overrides) -> RepoConfig:
    fields = {"full_name": "o/sandbox", "role": "sandbox", "allow_apply": True}
    return RepoConfig(**{**fields, "allow_fix_prs": True, **overrides})


def _gates(repo: RepoConfig, **overrides):
    kwargs = {"apply_flag": True, "apply_changes_env": "true", "token_available": True}
    return check_write_gates(repo, **{**kwargs, **overrides})


def test_approve_fix_pr_needs_all_five_gates_the_flag_and_a_human_approval():
    repo = _fix_repo()
    ok = approve_fix_pr(
        repo, "abc123abc123", write_gates=_gates(repo), human_approved_ids={"abc123abc123"}
    )
    assert ok.passed and ok.repo == "o/sandbox" and ok.proposal_id == "abc123abc123"

    cases = [
        (repo, {"apply_flag": False}, {"abc123abc123"}, "--apply"),
        (repo, {"apply_changes_env": "false"}, {"abc123abc123"}, "APPLY_CHANGES"),
        (repo, {"token_available": False}, {"abc123abc123"}, "token"),
        (_fix_repo(allow_apply=False), {}, {"abc123abc123"}, "allow_apply"),
        (_fix_repo(allow_fix_prs=False), {}, {"abc123abc123"}, "allow_fix_prs"),
        (repo, {}, set(), "approved by a human"),
        (repo, {}, {"000000000000"}, "approved by a human"),
    ]
    for r, gate_overrides, approved, reason in cases:
        result = approve_fix_pr(
            r, "abc123abc123", write_gates=_gates(r, **gate_overrides), human_approved_ids=approved
        )
        assert not result.passed and any(reason in x for x in result.reasons), reason
