"""Tests for scripts.seed_sandbox's pure data-generation functions (§13, §11).

This deliberately never calls ``main()`` or ``create_issue``/``create_pr_branch``
-- those make real HTTP writes, and this script only ever runs when a human
invokes it with --confirm (see DECISIONS.md/STATUS.md). Only the content the
script *would* create is tested.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import scripts.seed_sandbox as seed
from scripts.seed_sandbox import (
    PACKAGE_DIR,
    SEED_ISSUE_KINDS,
    build_fixable_bugs,
    build_issues,
    build_prs,
    package_files,
)

CHECKS = Path(__file__).parents[2] / "evals" / "repo_maint" / "fix_fixtures" / "checks"


def test_build_issues_creates_roughly_25():
    issues = build_issues()
    assert 24 <= len(issues) <= 30


def test_seeded_issues_cover_every_required_kind():
    kinds = {issue.kind for issue in build_issues() + [b.issue for b in build_fixable_bugs()]}
    assert kinds == set(SEED_ISSUE_KINDS)


def test_build_issues_has_exactly_one_injection_attempt():
    injections = [i for i in build_issues() if i.kind == "injection"]
    assert len(injections) == 1
    assert "ignore" in injections[0].body.lower()


def test_build_issues_has_three_duplicate_pairs():
    duplicates = [i for i in build_issues() if i.kind == "duplicate"]
    assert len(duplicates) == 6  # 3 pairs


def test_build_issues_titles_are_unique():
    titles = [i.title for i in build_issues()]
    assert len(titles) == len(set(titles))


def test_build_prs_creates_three_with_one_stale():
    prs = build_prs()
    assert len(prs) == 3
    stale = [pr for pr in prs if pr.make_stale]
    assert len(stale) == 1
    assert stale[0].request_changes is True


def test_five_fixable_bugs_each_point_at_a_real_module_and_a_hidden_check():
    bugs = build_fixable_bugs()
    assert [b.eval_id for b in bugs] == [f"fix-0{i}" for i in range(1, 6)]
    files = package_files()
    for b in bugs:
        assert b.module in files, b.module
        assert (CHECKS / f"test_{b.eval_id.replace('-', '_')}.py").is_file()
        assert "Steps to reproduce" in b.issue.body and "Expected" in b.issue.body
    titles = [i.title for i in build_issues()] + [b.issue.title for b in bugs]
    assert len(titles) == len(set(titles))


def test_package_files_are_the_sandbox_package_without_caches():
    files = package_files()
    assert "pyproject.toml" in files and "ledgerlite/money.py" in files
    assert "tests/test_ledgerlite.py" in files
    assert not any("__pycache__" in p for p in files)
    assert PACKAGE_DIR.name == "sandbox_package"


def test_main_refuses_without_confirm_and_is_never_imported_by_the_agent(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["seed_sandbox", "--repo", "o/sandbox"])
    assert seed.main() == 1
    assert "Refusing" in capsys.readouterr().out
    agents_dir = Path(__file__).parents[2] / "agents"
    for path in agents_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("scripts"), path
    assert "--confirm" in inspect.getsource(seed.main)
