"""LLM issue triage: post-processing, label allowlisting, priority escalation,
duplicate confirmation, caching, and a narrow number guard (§7.2, §11).

Prompt construction and the actual model call are the caller's job via an
injected ``classify_fn`` -- this module owns everything code-side: it never
trusts a raw model field without validating it against an allowlist or a
closed set of valid values, and writes are decided from these validated
fields elsewhere (``actions.py``), never from free text (§8.5).

``contains_number_support`` below is a narrow, triage-specific check (do
the numbers in a generated sentence appear in the source issue text?). It
is **not** a reimplementation of ``agents_core.guards.verify_numbers``,
which is considerably more sophisticated (scale suffixes, percent/bp,
dates, versions, etc.) and isn't installable yet -- see DECISIONS.md and
STATUS.md. Swap this for the real guard once agents-core is wired in.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents.repo_maint.config import RepoConfig

PROMPT_VERSION = "v1"

CLASSIFICATIONS = {"bug", "feature", "question", "docs", "chore", "other"}
PRIORITIES = ["p0", "p1", "p2", "p3"]  # most urgent first
CONFIDENCES = {"low", "medium", "high"}
MISSING_INFO_OPTIONS = {
    "steps to reproduce",
    "expected vs actual",
    "version",
    "environment",
    "logs or console output",
    "screenshot",
}
MAX_SUGGESTED_LABELS = 3

#: §7.2 post-processing: force priority to at least p1 only for bugs whose
#: issue text matches this regex. The model's own priority is otherwise kept.
SECURITY_REGEX = re.compile(r"security|vulnerability|data loss|crash on start", re.IGNORECASE)

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class TriageInput:
    issue: dict[str, Any]
    candidates: list[dict[str, Any]]  # [{number, title, snippet, state}]


@dataclass
class TriageResult:
    number: int
    classification: str
    priority: str
    confidence: str
    suggested_labels: list[str] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    summary: str | None = None
    first_response: str | None = None
    duplicates: list[dict[str, Any]] = field(default_factory=list)


ClassifyFn = Callable[[TriageInput], dict[str, Any]]


def content_hash(issue: dict[str, Any]) -> str:
    """sha256(title + body), for triage cache invalidation (state.json)."""
    payload = f"{issue.get('title', '')}\n{issue.get('body') or ''}".encode()
    return hashlib.sha256(payload).hexdigest()


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def escalate_priority(classification: str, priority: str, issue_text: str) -> str:
    """Force priority to at least p1 for bugs matching the security/data-loss
    regex; otherwise keep the model's priority as-is (§7.2)."""
    if classification != "bug" or not SECURITY_REGEX.search(issue_text):
        return priority
    if _priority_rank(priority) > _priority_rank("p1"):
        return "p1"
    return priority


def filter_labels(raw_labels: list[Any], allowlist: set[str]) -> list[str]:
    """Only labels in the allowlist survive, at most 3 (§7.2, §8.2)."""
    return [label for label in raw_labels if isinstance(label, str) and label in allowlist][
        :MAX_SUGGESTED_LABELS
    ]


def map_classification_to_label(classification: str, label_map: dict[str, str]) -> str | None:
    return label_map.get(classification)


def filter_duplicates(
    raw_duplicates: list[Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Only candidates the model confirmed (``duplicate_likely: true``) that
    were actually offered survive, enriched with the candidate's own data."""
    by_number = {c["number"]: c for c in candidates}
    confirmed = []
    for entry in raw_duplicates:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if entry.get("duplicate_likely") and number in by_number:
            confirmed.append({**by_number[number], "duplicate_likely": True})
    return confirmed


def contains_number_support(text: str, source_text: str) -> bool:
    """Every number-looking token in ``text`` also appears in ``source_text``."""
    return set(_NUMBER_RE.findall(text)) <= set(_NUMBER_RE.findall(source_text))


def postprocess(
    raw: dict[str, Any],
    issue: dict[str, Any],
    candidates: list[dict[str, Any]],
    existing_labels: set[str],
    label_map: dict[str, str],
) -> TriageResult:
    """Validate every field of a raw model response against an allowlist or
    a closed set of valid values (§7.2, §8.5) -- nothing here trusts the
    model's output directly."""
    issue_text = f"{issue.get('title', '')}\n{issue.get('body') or ''}"

    classification = raw.get("classification")
    if classification not in CLASSIFICATIONS:
        classification = "other"

    confidence = raw.get("confidence")
    if confidence not in CONFIDENCES:
        confidence = "low"

    priority = raw.get("priority")
    if priority not in PRIORITIES:
        priority = "p2"
    priority = escalate_priority(classification, priority, issue_text)

    raw_labels = raw.get("suggested_labels")
    raw_labels = raw_labels if isinstance(raw_labels, list) else []
    suggested_labels = filter_labels(raw_labels, existing_labels)
    mapped_label = map_classification_to_label(classification, label_map)
    if mapped_label and mapped_label in existing_labels and mapped_label not in suggested_labels:
        suggested_labels = (suggested_labels + [mapped_label])[:MAX_SUGGESTED_LABELS]

    missing_info = [m for m in (raw.get("missing_info") or []) if m in MISSING_INFO_OPTIONS]

    duplicates = filter_duplicates(raw.get("duplicates") or [], candidates)

    summary = raw.get("summary")
    if not isinstance(summary, str) or not contains_number_support(summary, issue_text):
        summary = None

    first_response = raw.get("first_response")
    if not isinstance(first_response, str) or not contains_number_support(
        first_response, issue_text
    ):
        first_response = None

    return TriageResult(
        number=issue["number"],
        classification=classification,
        priority=priority,
        confidence=confidence,
        suggested_labels=suggested_labels,
        missing_info=missing_info,
        summary=summary,
        first_response=first_response,
        duplicates=duplicates,
    )


def triage_issue(
    issue: dict[str, Any],
    candidates: list[dict[str, Any]],
    existing_labels: set[str],
    repo: RepoConfig,
    cache_entry: dict[str, Any] | None,
    classify_fn: ClassifyFn | None,
) -> tuple[TriageResult | None, bool]:
    """Returns ``(result, cached)``. ``result`` is ``None`` when there's no
    usable cache and no ``classify_fn`` was injected (no LLM wired
    tonight -- see DECISIONS.md): nothing to publish for this issue yet."""
    issue_hash = content_hash(issue)
    if (
        cache_entry
        and cache_entry.get("hash") == issue_hash
        and cache_entry.get("prompt_version") == PROMPT_VERSION
    ):
        return TriageResult(**cache_entry["result"]), True

    if classify_fn is None:
        return None, False

    raw = classify_fn(TriageInput(issue=issue, candidates=candidates))
    result = postprocess(raw, issue, candidates, existing_labels, repo.label_map)
    return result, False


def triage_repo(
    untriaged_issues: list[dict[str, Any]],
    candidates_by_number: dict[int, list[dict[str, Any]]],
    existing_labels: set[str],
    repo: RepoConfig,
    triage_cache: dict[str, Any],
    classify_fn: ClassifyFn | None,
    max_per_run: int,
) -> list[tuple[TriageResult | None, bool]]:
    """Triage every untriaged issue, respecting ``max_triage_per_repo_per_run``
    (§7.1): once the cap of fresh (non-cached) calls is hit, remaining
    issues are skipped for this run (they'll be picked up next run)."""
    results: list[tuple[TriageResult | None, bool]] = []
    fresh_calls_made = 0
    for issue in untriaged_issues:
        cache_entry = triage_cache.get(str(issue["number"]))
        issue_hash = content_hash(issue)
        is_cache_hit = bool(
            cache_entry
            and cache_entry.get("hash") == issue_hash
            and cache_entry.get("prompt_version") == PROMPT_VERSION
        )
        if not is_cache_hit and fresh_calls_made >= max_per_run:
            results.append((None, False))
            continue

        candidates = candidates_by_number.get(issue["number"], [])
        result, cached = triage_issue(
            issue, candidates, existing_labels, repo, cache_entry, classify_fn
        )
        if not is_cache_hit and result is not None:
            fresh_calls_made += 1
        results.append((result, cached))
    return results
