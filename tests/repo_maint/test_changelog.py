"""Tests for agents.repo_maint.changelog (§5.5, §7.3, §11)."""

from __future__ import annotations

from datetime import UTC, datetime

from agents.repo_maint.changelog import (
    BaseRef,
    ChangelogItem,
    build_changelog,
    check_refs,
    deterministic_markdown,
    draft_changelog,
    pr_set_hash,
    select_base,
    select_commits,
    select_pull_requests,
    suggest_version,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def pr(number, title, merged_at, labels=None, base_ref="main", body=""):
    return {
        "number": number,
        "title": title,
        "merged_at": merged_at,
        "labels": [{"name": label} for label in (labels or [])],
        "base": {"ref": base_ref},
        "user": {"login": "dev"},
        "body": body,
    }


# -- base ref selection (§5.5 step 1) --------------------------------------------


def test_select_base_prefers_latest_release():
    release = {"tag_name": "v1.2.0", "published_at": "2026-08-01T00:00:00Z"}
    base = select_base(release, [], NOW)
    assert base.ref == "v1.2.0"
    assert base.source == "release"
    assert base.is_semver is True


def test_select_base_falls_back_to_latest_semver_tag():
    tags = [{"name": "v0.9.0"}, {"name": "v1.0.0", "date": "2026-07-01T00:00:00Z"}]
    base = select_base(None, tags, NOW)
    assert base.ref == "v1.0.0"
    assert base.source == "tag"


def test_select_base_falls_back_to_last_30_days():
    base = select_base(None, [], NOW)
    assert base.ref is None
    assert base.source == "30_days"
    assert base.is_semver is False
    assert (NOW - base.date).days == 30


# -- content: PR vs commit mode (§5.5 step 2) ------------------------------------


def test_select_pull_requests_filters_by_base_date_and_default_branch():
    base_date = datetime(2026, 8, 1, tzinfo=UTC)
    base = BaseRef(ref="v1.0.0", date=base_date, source="release", is_semver=True)
    prs = [
        pr(1, "Old PR", "2026-07-01T00:00:00Z"),  # before base date
        pr(2, "New PR", "2026-08-15T00:00:00Z"),  # after, on main
        pr(3, "Wrong branch", "2026-08-15T00:00:00Z", base_ref="dev"),  # wrong target
        pr(4, "Unmerged", None),
    ]
    items = select_pull_requests(prs, base, default_branch="main", max_items=80)
    assert [i.ref for i in items] == ["#2"]


def test_select_pull_requests_caps_at_max_items_and_sorts_by_merged_at():
    base_date = datetime(2025, 12, 1, tzinfo=UTC)
    base = BaseRef(ref="v1.0.0", date=base_date, source="release", is_semver=True)
    prs = [pr(n, f"PR {n}", f"2026-0{n}-01T00:00:00Z") for n in range(1, 6)]
    items = select_pull_requests(prs, base, default_branch="main", max_items=3)
    assert len(items) == 3
    assert [i.ref for i in items] == ["#1", "#2", "#3"]


def test_select_commits_used_for_solo_repos():
    commits = [
        {
            "sha": "a1b2c3d4e5",
            "commit": {
                "message": "Fix bug\n\ndetails",
                "author": {"name": "dev", "date": "2026-08-01T00:00:00Z"},
            },
        },
    ]
    items = select_commits(commits, max_items=80)
    assert items[0].ref == "a1b2c3d"
    assert items[0].title == "Fix bug"


# -- semver suggestion (§5.5 step 3) ---------------------------------------------


def test_suggest_version_major_from_breaking_label():
    base = BaseRef(ref="v1.2.3", date=NOW, source="release", is_semver=True)
    items = [ChangelogItem(ref="#1", title="Change API", labels=["breaking"])]
    assert suggest_version(items, base) == "2.0.0"


def test_suggest_version_major_from_conventional_bang():
    base = BaseRef(ref="v1.2.3", date=NOW, source="release", is_semver=True)
    items = [ChangelogItem(ref="a1b2c3d", title="feat!: remove old config format")]
    assert suggest_version(items, base) == "2.0.0"


def test_suggest_version_minor_from_feat_prefix():
    base = BaseRef(ref="v1.2.3", date=NOW, source="release", is_semver=True)
    items = [ChangelogItem(ref="#1", title="feat: add dark mode")]
    assert suggest_version(items, base) == "1.3.0"


def test_suggest_version_minor_from_enhancement_label():
    base = BaseRef(ref="v1.2.3", date=NOW, source="release", is_semver=True)
    items = [ChangelogItem(ref="#1", title="Add widget", labels=["enhancement"])]
    assert suggest_version(items, base) == "1.3.0"


def test_suggest_version_patch_otherwise():
    base = BaseRef(ref="v1.2.3", date=NOW, source="release", is_semver=True)
    items = [ChangelogItem(ref="#1", title="Fix typo", labels=["bug"])]
    assert suggest_version(items, base) == "1.2.4"


def test_suggest_version_null_when_base_not_semver():
    base = BaseRef(ref=None, date=NOW, source="30_days", is_semver=False)
    items = [ChangelogItem(ref="#1", title="feat: x")]
    assert suggest_version(items, base) is None


# -- pr set hash caching (§5.5 step 4) -------------------------------------------


def test_pr_set_hash_stable_under_reordering():
    a = [
        ChangelogItem(ref="#1", title="A", merged_at=NOW),
        ChangelogItem(ref="#2", title="B", merged_at=NOW),
    ]
    b = list(reversed(a))
    assert pr_set_hash(a) == pr_set_hash(b)


def test_pr_set_hash_changes_when_items_change():
    a = [ChangelogItem(ref="#1", title="A", merged_at=NOW)]
    b = [ChangelogItem(ref="#1", title="A changed", merged_at=NOW)]
    assert pr_set_hash(a) != pr_set_hash(b)


# -- §7.3 ref guard ---------------------------------------------------------------


def test_check_refs_ok_when_every_item_referenced_and_nothing_extra():
    items = [ChangelogItem(ref="#1", title="A"), ChangelogItem(ref="#2", title="B")]
    markdown = "### Added\n- A (#1)\n- B (#2)\n"
    result = check_refs(markdown, items)
    assert result.ok is True


def test_check_refs_detects_missing_item():
    items = [ChangelogItem(ref="#1", title="A"), ChangelogItem(ref="#2", title="B")]
    markdown = "### Added\n- A (#1)\n"
    result = check_refs(markdown, items)
    assert result.ok is False
    assert result.missing == {"#2"}


def test_check_refs_detects_invented_ref():
    items = [ChangelogItem(ref="#1", title="A")]
    markdown = "### Added\n- A (#1)\n- Something invented (#999)\n"
    result = check_refs(markdown, items)
    assert result.ok is False
    assert result.extra == {"#999"}


# -- deterministic fallback (§7.3 step 3) ----------------------------------------


def test_deterministic_markdown_groups_by_label():
    items = [
        ChangelogItem(ref="#1", title="Crash on load", labels=["bug"]),
        ChangelogItem(ref="#2", title="Add export", labels=["enhancement"]),
    ]
    markdown = deterministic_markdown(items, "## [Unreleased]")
    assert "### Fixed" in markdown
    assert "### Added" in markdown
    assert "Crash on load (#1)" in markdown
    assert "Add export (#2)" in markdown


def test_deterministic_markdown_groups_by_conventional_commit_prefix():
    items = [ChangelogItem(ref="a1b2c3d", title="fix: null pointer")]
    markdown = deterministic_markdown(items, "## [Unreleased]")
    assert "### Fixed" in markdown


def test_deterministic_markdown_omits_empty_groups():
    items = [ChangelogItem(ref="#1", title="Add x", labels=["enhancement"])]
    markdown = deterministic_markdown(items, "## [Unreleased]")
    assert "### Fixed" not in markdown
    assert "### Security" not in markdown


# -- draft_changelog orchestration -----------------------------------------------


def test_draft_changelog_with_no_llm_goes_straight_to_deterministic():
    items = [ChangelogItem(ref="#1", title="fix: bug", labels=["bug"])]
    markdown, source = draft_changelog(items, "## [Unreleased]", draft_fn=None)
    assert source == "deterministic"
    assert "### Fixed" in markdown


def test_draft_changelog_accepts_llm_output_on_first_try():
    items = [ChangelogItem(ref="#1", title="A")]

    def draft_fn(heading, its, retry):
        assert retry is None
        return f"{heading}\n- A (#1)\n"

    markdown, source = draft_changelog(items, "## [Unreleased]", draft_fn=draft_fn)
    assert source == "llm"
    assert "(#1)" in markdown


def test_draft_changelog_retries_once_then_falls_back_to_deterministic():
    items = [ChangelogItem(ref="#1", title="A", labels=["bug"])]
    calls = []

    def draft_fn(heading, its, retry):
        calls.append(retry)
        return f"{heading}\n- made up thing (#999)\n"  # always wrong

    markdown, source = draft_changelog(items, "## [Unreleased]", draft_fn=draft_fn)
    assert len(calls) == 2  # one retry, per §7.3
    assert calls[0] is None
    assert calls[1] is not None and calls[1].ok is False
    assert source == "deterministic"
    assert "### Fixed" in markdown


def test_draft_changelog_succeeds_on_retry():
    items = [ChangelogItem(ref="#1", title="A")]
    attempts = {"n": 0}

    def draft_fn(heading, its, retry):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return f"{heading}\n- wrong (#999)\n"
        return f"{heading}\n- A (#1)\n"

    markdown, source = draft_changelog(items, "## [Unreleased]", draft_fn=draft_fn)
    assert source == "llm"
    assert attempts["n"] == 2


# -- build_changelog orchestration + caching -------------------------------------


def test_build_changelog_uses_cache_when_hash_and_base_unchanged():
    base = BaseRef(ref="v1.0.0", date=NOW, source="release", is_semver=True)
    items = [ChangelogItem(ref="#1", title="A", merged_at=NOW)]
    cache = {
        "base_ref": "v1.0.0",
        "pr_set_hash": pr_set_hash(items),
        "markdown": "cached markdown",
        "narrative_source": "llm",
    }
    result = build_changelog(
        base=base, items=items, content_source="pull_requests", version_heading="## X", cache=cache
    )
    assert result.cached is True
    assert result.markdown == "cached markdown"


def test_build_changelog_ignores_cache_when_hash_changed():
    base = BaseRef(ref="v1.0.0", date=NOW, source="release", is_semver=True)
    items = [ChangelogItem(ref="#1", title="A", labels=["bug"], merged_at=NOW)]
    cache = {"base_ref": "v1.0.0", "pr_set_hash": "stale-hash", "markdown": "cached markdown"}
    result = build_changelog(
        base=base, items=items, content_source="pull_requests", version_heading="## X", cache=cache
    )
    assert result.cached is False
    assert "### Fixed" in result.markdown
