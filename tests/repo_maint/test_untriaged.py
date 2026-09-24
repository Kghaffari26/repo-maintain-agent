"""Tests for agents.repo_maint.untriaged (§5.1, §11)."""

from __future__ import annotations

from agents.repo_maint.config import RepoConfig
from agents.repo_maint.untriaged import filter_untriaged, has_maintainer_comment, is_untriaged

REPO = RepoConfig(
    full_name="you/repo",
    role="own",
    label_map={"bug": "bug", "feature": "enhancement"},
    priority_labels={"p0": "priority: critical"},
    extra_triaged_labels=["triaged"],
    ignore_labels=["wontfix", "on-hold"],
)


def issue(number=1, labels=None, created_at="2026-01-01T00:00:00Z"):
    return {
        "number": number,
        "title": "Something broke",
        "labels": [{"name": name} for name in (labels or [])],
        "created_at": created_at,
        "user": {"login": "reporter"},
    }


def comment(login="maintainer", association="OWNER", body="looks good", bot=False):
    return {
        "user": {"login": login, "type": "Bot" if bot else "User"},
        "author_association": association,
        "body": body,
        "created_at": "2026-01-02T00:00:00Z",
    }


def test_untriaged_by_default():
    assert is_untriaged(issue(), [], REPO) is True


def test_not_untriaged_when_labeled_bug():
    assert is_untriaged(issue(labels=["bug"]), [], REPO) is False


def test_not_untriaged_when_priority_label_present():
    assert is_untriaged(issue(labels=["priority: critical"]), [], REPO) is False


def test_not_untriaged_when_extra_triaged_label_present():
    assert is_untriaged(issue(labels=["triaged"]), [], REPO) is False


def test_not_untriaged_when_ignore_label_present():
    assert is_untriaged(issue(labels=["wontfix"]), [], REPO) is False


def test_not_untriaged_when_maintainer_commented():
    comments = [comment(association="OWNER")]
    assert is_untriaged(issue(), comments, REPO) is False


def test_still_untriaged_when_only_non_maintainer_commented():
    comments = [comment(login="rando", association="NONE")]
    assert is_untriaged(issue(), comments, REPO) is True


def test_still_untriaged_when_only_bot_commented_as_owner():
    # a bot account, even if GitHub reports it with OWNER association, doesn't count
    comments = [comment(login="ci-bot[bot]", association="OWNER", bot=True)]
    assert is_untriaged(issue(), comments, REPO) is True


def test_still_untriaged_when_only_agents_own_marker_comment_present():
    comments = [comment(association="OWNER", body="<!-- agents-hub:triage v1 -->\nhi")]
    assert is_untriaged(issue(), comments, REPO) is True


def test_untriaged_after_maintainer_marker_then_real_comment_is_still_triaged():
    comments = [
        comment(association="OWNER", body="<!-- agents-hub:triage v1 -->\nhi"),
        comment(association="MEMBER", body="I'll take a look"),
    ]
    assert is_untriaged(issue(), comments, REPO) is False


def test_has_maintainer_comment_true_for_collaborator():
    assert has_maintainer_comment([comment(association="COLLABORATOR")]) is True


def test_filter_untriaged_skips_pull_requests():
    pr = {**issue(number=2), "pull_request": {"url": "..."}}
    result = filter_untriaged([issue(number=1), pr], {}, REPO)
    assert [i["number"] for i in result] == [1]


def test_filter_untriaged_uses_comments_by_number():
    comments_by_number = {1: [comment(association="OWNER")]}
    result = filter_untriaged([issue(number=1), issue(number=2)], comments_by_number, REPO)
    assert [i["number"] for i in result] == [2]
