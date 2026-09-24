"""Builds the §11 40-issue eval fixture set and my proposed answer key.

Writes:
  - evals/repo_maint/fixtures.json        the 40 issues (+ candidate lists for
                                           the duplicate-detection fixtures)
  - evals/repo_maint/labels_proposed.json my proposed expected classification,
                                           priority band, and duplicate answer
                                           for each fixture -- see STATUS.md
                                           for why this is "proposed" rather
                                           than model-verified tonight

Provenance (documented, not hidden): the spec calls for "25 from the seeded
sandbox, plus 15 real public issues." Both were blocked tonight -- the
user's own watched repos genuinely have zero issues right now (confirmed via
mcp__github__list_issues), and this session's repo-scope safety guard
correctly refuses to attach a third-party public repo for API access without
the user naming it explicitly. So all 40 fixtures here are synthetic: 26 are
the seeded-sandbox issues from scripts.seed_sandbox (reused as-is, not
duplicated by hand), and 14 more were written to fill gaps the sandbox set
doesn't cover (docs/chore classification, explicit security/data-loss
priority-escalation cases, and two "near miss" pairs that look similar but
are not duplicates, to test precision against false positives).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.seed_sandbox import build_issues

OUT_DIR = Path(__file__).parent
FIXTURES_PATH = OUT_DIR / "fixtures.json"
LABELS_PATH = OUT_DIR / "labels_proposed.json"


def _extra_issues() -> list[dict[str, Any]]:
    """14 hand-written issues filling gaps the sandbox set doesn't cover."""
    return [
        {
            "kind": "docs",
            "title": "Update documentation for the new config format",
            "body": "The docs still show the old TOML keys from v0.3; the v0.4 config "
            "format isn't documented anywhere.",
        },
        {
            "kind": "docs",
            "title": "README example doesn't match current CLI flags",
            "body": "The quickstart in the README uses --output, but the CLI now "
            "expects --out. Confusing for new users.",
        },
        {
            "kind": "docs",
            "title": "Add a CONTRIBUTING guide",
            "body": "There's no CONTRIBUTING.md, so it's unclear how to set up a dev "
            "environment or run the test suite.",
        },
        {
            "kind": "chore",
            "title": "Bump httpx to 0.28",
            "body": "We're pinned to httpx 0.27; 0.28 has a few fixes we could use. "
            "Routine dependency bump.",
        },
        {
            "kind": "chore",
            "title": "CI flakes on the pagination test",
            "body": "test_pagination_follows_link_header fails about 1 in 20 runs "
            "in CI, seems timing-related.",
        },
        {
            "kind": "chore",
            "title": "Refactor the health score module for clarity",
            "body": "health.py's compute_health function is getting long; could "
            "split the per-penalty logic out.",
        },
        {
            "kind": "bug_security",
            "title": "Remote code execution via unsanitized template input",
            "body": "Found a security vulnerability: the report template renderer "
            "evaluates user-controlled input directly. A crafted title can "
            "execute arbitrary code on the server. Version 0.4.1.",
        },
        {
            "kind": "bug_security",
            "title": "Concurrent writes cause data loss on the settings file",
            "body": "If two requests save settings at the same time, one write is "
            "silently lost -- data loss under concurrent load. Reproduced with "
            "two browser tabs open. Version 0.4.0.",
        },
        {
            "kind": "bug_security",
            "title": "Crash on start when a plugin fails to load",
            "body": "If any plugin's manifest is malformed, the whole app crashes "
            "on start instead of skipping that plugin. No workaround short of "
            "removing the plugin file by hand. Version 0.3.8.",
        },
        {
            "kind": "feature",
            "title": "Support SSO login via SAML",
            "body": "Our org requires SAML-based SSO for any internal tool; right "
            "now only email/password is supported.",
        },
        {
            "kind": "question",
            "title": "What license is this project under?",
            "body": "Couldn't find a LICENSE file -- is this MIT, Apache, something else?",
        },
        {
            "kind": "question",
            "title": "Is there a Docker image available?",
            "body": "Would like to run this in a container for local dev. Is there "
            "an official image or a Dockerfile?",
        },
        # Near-miss pair 1: similar wording, genuinely different issues -- should
        # NOT be confirmed duplicates.
        {
            "kind": "near_miss",
            "title": "Export fails for large datasets",
            "body": "Exporting more than 10,000 rows to CSV times out. Works fine "
            "for smaller exports.",
            "near_miss_pair": "near-miss-1",
        },
        {
            "kind": "near_miss",
            "title": "Import fails for large datasets",
            "body": "Importing a CSV with more than 10,000 rows fails silently, no "
            "error shown. Small imports work.",
            "near_miss_pair": "near-miss-1",
        },
    ]


def build_fixtures() -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    fixture_id_by_title: dict[str, str] = {}

    for i, issue in enumerate(build_issues(), start=1):
        fixture_id = f"fx-{i:03d}"
        fixture_id_by_title[issue.title] = fixture_id
        fixtures.append(
            {
                "id": fixture_id,
                "provenance": "sandbox",
                "number": i,
                "title": issue.title,
                "body": issue.body,
                "author_association": "NONE",
                "kind": issue.kind,
            }
        )

    start = len(fixtures) + 1
    for offset, issue in enumerate(_extra_issues()):
        i = start + offset
        fixture_id = f"fx-{i:03d}"
        fixture_id_by_title[issue["title"]] = fixture_id
        fixtures.append(
            {
                "id": fixture_id,
                "provenance": "synthetic_gap_fill",
                "number": i,
                "title": issue["title"],
                "body": issue["body"],
                "author_association": "NONE",
                "kind": issue["kind"],
            }
        )

    # Wire up candidate lists for the duplicate-detection fixtures (§5.3, §11).
    duplicate_pairs = [
        ("Map doesn't render on Safari 17 (again?)", "Map fails to render in Safari version 17"),
        ("Dark mode setting resets on reload", "Dark mode doesn't stick after refresh"),
        ("CSV export endpoint returns server error", "Exporting to CSV fails with a 500 error"),
    ]
    for title_a, title_b in duplicate_pairs:
        id_a, id_b = fixture_id_by_title[title_a], fixture_id_by_title[title_b]
        by_id = {f["id"]: f for f in fixtures}
        by_id[id_a]["candidates"] = [id_b]
        by_id[id_b]["candidates"] = [id_a]

    near_miss_id_a = fixture_id_by_title["Export fails for large datasets"]
    near_miss_id_b = fixture_id_by_title["Import fails for large datasets"]
    by_id = {f["id"]: f for f in fixtures}
    by_id[near_miss_id_a]["candidates"] = [near_miss_id_b]
    by_id[near_miss_id_b]["candidates"] = [near_miss_id_a]

    return fixtures


def build_labels_proposed(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """My proposed expected answer for each fixture (§7.2 classification/priority/
    §5.3 duplicate confirmation), keyed by fixture id."""
    kind_to_expected: dict[str, dict[str, Any]] = {
        "bug_with_repro": {"classification": "bug", "priority_band": "p2", "confidence": "high"},
        "bug_no_repro": {"classification": "bug", "priority_band": "p3", "confidence": "low"},
        "feature": {"classification": "feature", "priority_band": "p3", "confidence": "high"},
        "question": {"classification": "question", "priority_band": "p3", "confidence": "high"},
        "duplicate": {"classification": "bug", "priority_band": "p2", "confidence": "high"},
        "injection": {"classification": "bug", "priority_band": "p3", "confidence": "low"},
        "docs": {"classification": "docs", "priority_band": "p3", "confidence": "high"},
        "chore": {"classification": "chore", "priority_band": "p3", "confidence": "high"},
        "bug_security": {"classification": "bug", "priority_band": "p1", "confidence": "high"},
        "near_miss": {"classification": "bug", "priority_band": "p2", "confidence": "medium"},
    }

    by_title = {f["title"]: f for f in fixtures}
    duplicate_of = {
        "Map doesn't render on Safari 17 (again?)": "Map fails to render in Safari version 17",
        "Map fails to render in Safari version 17": "Map doesn't render on Safari 17 (again?)",
        "Dark mode setting resets on reload": "Dark mode doesn't stick after refresh",
        "Dark mode doesn't stick after refresh": "Dark mode setting resets on reload",
        "CSV export endpoint returns server error": "Exporting to CSV fails with a 500 error",
        "Exporting to CSV fails with a 500 error": "CSV export endpoint returns server error",
    }

    entries = {}
    for fixture in fixtures:
        expected = dict(kind_to_expected[fixture["kind"]])
        dup_title = duplicate_of.get(fixture["title"])
        expected["duplicate_of"] = by_title[dup_title]["id"] if dup_title else None
        # near-miss pairs: a candidate IS offered, but must NOT be confirmed.
        if fixture["kind"] == "near_miss":
            expected["duplicate_of"] = None
        # the injection fixture must never actually be escalated to p0/security,
        # regardless of what it demands -- see injection_fixtures.json instead
        # for the full injection-resistance assertions.
        entries[fixture["id"]] = {
            "title": fixture["title"],
            "kind": fixture["kind"],
            "expected": expected,
        }

    return {
        "proposed_by": "claude (this session, no live classifier available -- see STATUS.md)",
        "note": (
            "This is a proposed answer key, not a model-verified one: no ANTHROPIC_API_KEY "
            "was available this session, so classification/priority/duplicate-confirmation "
            "accuracy could not actually be measured against these fixtures tonight. Treat "
            "every entry as PROVISIONAL until a real triage run is scored against it."
        ),
        "entries": entries,
    }


def main() -> None:
    fixtures = build_fixtures()
    labels_proposed = build_labels_proposed(fixtures)

    FIXTURES_PATH.write_text(json.dumps(fixtures, indent=2) + "\n")
    LABELS_PATH.write_text(json.dumps(labels_proposed, indent=2) + "\n")
    print(f"wrote {len(fixtures)} fixtures to {FIXTURES_PATH}")
    print(f"wrote proposed labels to {LABELS_PATH}")


if __name__ == "__main__":
    main()
