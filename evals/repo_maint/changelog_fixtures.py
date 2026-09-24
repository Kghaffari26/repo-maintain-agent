"""3 fixture PR sets for the changelog-fidelity eval (§11).

Each fixture scripts a ``draft_fn`` response sequence to exercise a
different path through ``changelog.draft_changelog``'s §7.3 guard:
first-attempt success, a one-ref miss corrected on retry, and a
never-gets-it-right case that falls back to the deterministic grouping.
This is real, executable code (``run_evals.py`` actually runs it) but it
scripts *what the model would say*, not what it did say -- there's no
ANTHROPIC_API_KEY this session (see STATUS.md), so read the reported
"first-attempt rate" as a test of the guard/retry/fallback mechanics, not
a measurement of real model quality.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agents.repo_maint.changelog import ChangelogItem

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _items(*specs: tuple[str, str, list[str]]) -> list[ChangelogItem]:
    return [
        ChangelogItem(ref=ref, title=title, labels=labels, merged_at=NOW)
        for ref, title, labels in specs
    ]


CHANGELOG_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "chg-01",
        "label": "well-behaved model, correct on the first attempt",
        "items": _items(
            ("#101", "Add metro compare mode", ["enhancement"]),
            ("#102", "Fix crash when a filter is empty", ["bug"]),
            ("#103", "Bump lockfile", ["chore"]),
        ),
        "responses": [
            lambda heading, items, retry: heading
            + "\n### Added\n- Metro compare mode (#101)\n"
            + "### Fixed\n- Crash when a filter is empty (#102)\n"
            + "### Other\n- Bump lockfile (#103)\n",
        ],
    },
    {
        "id": "chg-02",
        "label": "misses one ref, corrects on the single allowed retry",
        "items": _items(
            ("#201", "Add dark mode toggle", ["enhancement"]),
            ("a1b2c3d", "docs: fix typo in README", []),
        ),
        "responses": [
            lambda heading, items, retry: heading + "\n### Added\n- Dark mode toggle (#201)\n",
            lambda heading, items, retry: heading
            + "\n### Added\n- Dark mode toggle (#201)\n"
            + "### Other\n- Fix typo in README (a1b2c3d)\n",
        ],
    },
    {
        "id": "chg-03",
        "label": "never gets the refs right -- falls back to deterministic grouping",
        "items": _items(
            ("#301", "Fix null pointer on empty response", ["bug"]),
            ("#302", "Add webhook support", ["enhancement"]),
        ),
        "responses": [
            lambda heading, items, retry: heading + "\n### Fixed\n- Something (#999)\n",
            lambda heading, items, retry: heading + "\n### Fixed\n- Something else entirely\n",
        ],
    },
]


def make_draft_fn(responses: list) -> Any:
    """Turns a fixture's scripted response list into a draft_fn: returns
    responses[0] on the first call, responses[1] on the retry, and repeats
    the last one if draft_changelog somehow calls it more than twice."""
    calls: list[int] = []

    def draft_fn(heading: str, items: list[ChangelogItem], retry: Any) -> str:
        index = min(len(calls), len(responses) - 1)
        calls.append(index)
        return responses[index](heading, items, retry)

    return draft_fn
