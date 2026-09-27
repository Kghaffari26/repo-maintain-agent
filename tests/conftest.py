"""Every test writes agents-core's local run state (cost log, guard-failure log,
eval results) under a temp dir, never into the repo's own data/ or evals/."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_agents_core_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTS_CORE_EVALS_DIR", str(tmp_path / "evals"))
    monkeypatch.setenv("AGENTS_CORE_PUBLISH_DIR", str(tmp_path / "public-data"))
