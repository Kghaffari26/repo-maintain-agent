"""Tests for scripts.seed_sandbox's pure data-generation functions (§13, §11).

This deliberately never calls ``main()`` or ``create_issue``/``create_pr_branch``
-- those make real HTTP writes and this script is not run tonight (see
DECISIONS.md/STATUS.md). Only the content the script *would* create is tested.
"""

from __future__ import annotations

from scripts.seed_sandbox import SEED_ISSUE_KINDS, build_issues, build_prs


def test_build_issues_creates_roughly_25():
    issues = build_issues()
    assert 24 <= len(issues) <= 30


def test_build_issues_covers_every_required_kind():
    kinds = {issue.kind for issue in build_issues()}
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
