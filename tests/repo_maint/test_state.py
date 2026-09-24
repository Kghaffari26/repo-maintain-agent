"""Tests for agents.repo_maint.state (§4, §11)."""

from __future__ import annotations

from pathlib import Path

from agents.repo_maint.state import RepoState, State, get_repo_state, load_state, save_state


def test_load_state_returns_empty_state_when_file_missing(tmp_path: Path):
    state = load_state(tmp_path / "does-not-exist.json")
    assert state.repos == {}


def test_save_and_load_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    state = State(
        repos={
            "you/repo": RepoState(
                etags={"issues_open": 'W/"abc"'},
                triage_cache={"42": {"hash": "deadbeef", "result": {}, "prompt_version": "v1"}},
                changelog_cache={"base_ref": "v0.3.0", "pr_set_hash": "abc", "markdown": "## x"},
                commented=[42, 57],
                labeled={"42": ["bug"]},
            )
        }
    )
    save_state(path, state)
    reloaded = load_state(path)

    assert reloaded.repos["you/repo"].etags == {"issues_open": 'W/"abc"'}
    assert reloaded.repos["you/repo"].commented == [42, 57]
    assert reloaded.repos["you/repo"].labeled == {"42": ["bug"]}
    assert reloaded.repos["you/repo"].triage_cache["42"]["hash"] == "deadbeef"


def test_save_state_creates_parent_directories(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "state.json"
    save_state(path, State())
    assert path.exists()


def test_get_repo_state_creates_entry_when_missing():
    state = State()
    repo_state = get_repo_state(state, "you/new-repo")
    assert isinstance(repo_state, RepoState)
    assert "you/new-repo" in state.repos


def test_get_repo_state_reuses_existing_entry():
    state = State(repos={"you/repo": RepoState(commented=[1])})
    repo_state = get_repo_state(state, "you/repo")
    assert repo_state.commented == [1]
