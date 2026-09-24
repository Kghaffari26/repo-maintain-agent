"""Changelog base-ref selection, content assembly, LLM draft guard, and
deterministic fallback (SPEC_REPO_MAINT.md §5.5, §7.3).

The "smart" narrative step is injected as ``draft_fn`` -- see DECISIONS.md:
no ANTHROPIC_API_KEY is present tonight, so every real run goes straight to
the deterministic fallback. That's not a shortcut around the spec: §7.3
step 3 *is* the deterministic grouping, used whenever the LLM path is
unavailable or fails its guard twice.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agents.repo_maint.metrics import parse_dt

DEFAULT_LOOKBACK_DAYS = 30
REF_RE = re.compile(r"#\d+|\b[0-9a-f]{7,40}\b")

BREAKING_LABELS = {"breaking"}
MINOR_LABELS = {"enhancement", "feature"}
_CONVENTIONAL_RE = re.compile(r"^(\w+)(\([^)]*\))?(!)?:")

LABEL_TO_GROUP = {
    "bug": "Fixed",
    "enhancement": "Added",
    "feature": "Added",
    "breaking": "Changed",
    "security": "Security",
    "documentation": "Other",
    "chore": "Other",
}
PREFIX_TO_GROUP = {"feat": "Added", "fix": "Fixed", "docs": "Other", "chore": "Other"}
GROUP_ORDER = ["Added", "Changed", "Fixed", "Removed", "Security", "Other"]


def _semver_key(tag_name: str) -> tuple[int, int, int] | None:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag_name)
    if not match:
        return None
    a, b, c = match.groups()
    return int(a), int(b), int(c)


# -- §5.5 step 1: base ref selection ------------------------------------------


@dataclass
class BaseRef:
    ref: str | None  # tag name, or None for the 30-day fallback
    date: datetime
    source: str  # "release" | "tag" | "30_days"
    is_semver: bool


def select_base(
    latest_release: dict[str, Any] | None,
    tags: list[dict[str, Any]],
    now: datetime,
) -> BaseRef:
    """Latest release -> latest semver tag -> last 30 days (§5.5 step 1).

    ``tags`` entries may carry an optional ``date`` key (an ISO timestamp
    for the tag's commit, enriched by ``fetch.py``); without one, "now" is
    used as a conservative fallback so nothing appears artificially old.
    """
    if latest_release and latest_release.get("tag_name") and latest_release.get("published_at"):
        tag_name = latest_release["tag_name"]
        return BaseRef(
            ref=tag_name,
            date=parse_dt(latest_release["published_at"]),
            source="release",
            is_semver=_semver_key(tag_name) is not None,
        )

    semver_tags = [(tag, _semver_key(tag["name"])) for tag in tags if _semver_key(tag["name"])]
    if semver_tags:
        semver_tags.sort(key=lambda pair: pair[1], reverse=True)
        tag, _key = semver_tags[0]
        date = parse_dt(tag["date"]) if tag.get("date") else now
        return BaseRef(ref=tag["name"], date=date, source="tag", is_semver=True)

    fallback_date = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return BaseRef(ref=None, date=fallback_date, source="30_days", is_semver=False)


# -- §5.5 step 2: contents -----------------------------------------------------


@dataclass
class ChangelogItem:
    ref: str  # "#23" or a short sha like "a1b2c3d"
    title: str
    labels: list[str] = field(default_factory=list)
    body_excerpt: str = ""
    author: str = "unknown"
    merged_at: datetime | None = None


def select_pull_requests(
    merged_prs: list[dict[str, Any]], base: BaseRef, default_branch: str, max_items: int
) -> list[ChangelogItem]:
    items = []
    for pr in merged_prs:
        if not pr.get("merged_at"):
            continue
        if (pr.get("base") or {}).get("ref") != default_branch:
            continue
        merged_at = parse_dt(pr["merged_at"])
        if merged_at <= base.date:
            continue
        items.append(
            ChangelogItem(
                ref=f"#{pr['number']}",
                title=pr["title"],
                labels=[label["name"] for label in pr.get("labels", [])],
                body_excerpt=(pr.get("body") or "")[:400],
                author=(pr.get("user") or {}).get("login", "unknown"),
                merged_at=merged_at,
            )
        )
    items.sort(key=lambda item: item.merged_at)
    return items[:max_items]


def select_commits(commits: list[dict[str, Any]], max_items: int) -> list[ChangelogItem]:
    """Fallback content source for solo repos with no merged PRs (§5.5 step 2)."""
    items = []
    for commit in commits:
        commit_info = commit["commit"]
        message = commit_info["message"].splitlines()[0]
        author_login = (commit.get("author") or {}).get("login") or commit_info["author"]["name"]
        items.append(
            ChangelogItem(
                ref=commit["sha"][:7],
                title=message,
                author=author_login,
                merged_at=parse_dt(commit_info["author"]["date"]),
            )
        )
    return items[:max_items]


def pr_set_hash(items: list[ChangelogItem]) -> str:
    """§5.5 step 4: sha256 of the sorted (ref, title, merged_at) tuples."""
    tuples = sorted(
        (item.ref, item.title, item.merged_at.isoformat() if item.merged_at else "")
        for item in items
    )
    return hashlib.sha256(repr(tuples).encode()).hexdigest()


# -- §5.5 step 3: suggested next version ---------------------------------------


def suggest_version(items: list[ChangelogItem], base: BaseRef) -> str | None:
    if not base.is_semver or base.ref is None:
        return None
    key = _semver_key(base.ref)
    if key is None:
        return None
    major, minor, patch = key

    bump = "patch"
    for item in items:
        if set(item.labels) & BREAKING_LABELS:
            bump = "major"
            break
        match = _CONVENTIONAL_RE.match(item.title)
        if match and match.group(3) == "!":
            bump = "major"
            break
        if set(item.labels) & MINOR_LABELS or (match and match.group(1) == "feat"):
            bump = "minor"

    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# -- §7.3 ref guard -------------------------------------------------------------


@dataclass
class GuardResult:
    ok: bool
    missing: set[str]  # input items whose ref never appeared in the output
    extra: set[str]  # refs in the output that aren't in the input set


def extract_refs(text: str) -> set[str]:
    return set(REF_RE.findall(text))


def check_refs(markdown: str, items: list[ChangelogItem]) -> GuardResult:
    input_refs = {item.ref for item in items}
    output_refs = extract_refs(markdown)
    missing = input_refs - output_refs
    extra = output_refs - input_refs
    return GuardResult(ok=not missing and not extra, missing=missing, extra=extra)


# -- deterministic fallback (§7.3 step 3) ---------------------------------------


def _group_for(item: ChangelogItem) -> str:
    for label in item.labels:
        if label in LABEL_TO_GROUP:
            return LABEL_TO_GROUP[label]
    match = _CONVENTIONAL_RE.match(item.title)
    if match:
        prefix = PREFIX_TO_GROUP.get(match.group(1).lower())
        if prefix:
            return prefix
    return "Other"


def deterministic_markdown(items: list[ChangelogItem], version_heading: str) -> str:
    groups: dict[str, list[ChangelogItem]] = {group: [] for group in GROUP_ORDER}
    for item in items:
        groups[_group_for(item)].append(item)

    lines = [version_heading, ""]
    for group in GROUP_ORDER:
        entries = groups[group]
        if not entries:
            continue
        lines.append(f"### {group}")
        lines.extend(f"- {item.title} ({item.ref})" for item in entries)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -- orchestration ---------------------------------------------------------------

DraftFn = Callable[[str, list[ChangelogItem], "GuardResult | None"], str]


def draft_changelog(
    items: list[ChangelogItem],
    version_heading: str,
    draft_fn: DraftFn | None = None,
) -> tuple[str, str]:
    """Returns ``(markdown, narrative_source)``, ``narrative_source`` is "llm" or
    "deterministic". With no ``draft_fn`` injected, goes straight to the
    deterministic fallback (see module docstring)."""
    if draft_fn is None:
        return deterministic_markdown(items, version_heading), "deterministic"

    markdown = draft_fn(version_heading, items, None)
    result = check_refs(markdown, items)
    if result.ok:
        return markdown, "llm"

    markdown = draft_fn(version_heading, items, result)
    result = check_refs(markdown, items)
    if result.ok:
        return markdown, "llm"

    return deterministic_markdown(items, version_heading), "deterministic"


@dataclass
class ChangelogResult:
    base_ref: str | None
    base_date: str
    source: str  # "pull_requests" | "commits"
    item_count: int
    suggested_version: str | None
    markdown: str
    narrative_source: str  # "llm" | "deterministic"
    cached: bool


def build_changelog(
    *,
    base: BaseRef,
    items: list[ChangelogItem],
    content_source: str,
    version_heading: str,
    cache: dict[str, Any] | None,
    draft_fn: DraftFn | None = None,
) -> ChangelogResult:
    """Assembles the full §6 changelog block, reusing the cached draft when the
    PR/commit set hash is unchanged (§5.5 step 4)."""
    set_hash = pr_set_hash(items)
    if cache and cache.get("base_ref") == base.ref and cache.get("pr_set_hash") == set_hash:
        return ChangelogResult(
            base_ref=base.ref,
            base_date=base.date.date().isoformat(),
            source=content_source,
            item_count=len(items),
            suggested_version=suggest_version(items, base),
            markdown=cache["markdown"],
            narrative_source=cache.get("narrative_source", "llm"),
            cached=True,
        )

    markdown, narrative_source = draft_changelog(items, version_heading, draft_fn)
    return ChangelogResult(
        base_ref=base.ref,
        base_date=base.date.date().isoformat(),
        source=content_source,
        item_count=len(items),
        suggested_version=suggest_version(items, base),
        markdown=markdown,
        narrative_source=narrative_source,
        cached=False,
    )
