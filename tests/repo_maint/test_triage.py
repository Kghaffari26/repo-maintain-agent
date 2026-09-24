"""Tests for agents.repo_maint.triage (§7.2, §8.5, §11)."""

from __future__ import annotations

from agents.repo_maint.config import RepoConfig
from agents.repo_maint.triage import (
    PROMPT_VERSION,
    TriageInput,
    contains_number_support,
    content_hash,
    escalate_priority,
    filter_duplicates,
    filter_labels,
    postprocess,
    triage_issue,
    triage_repo,
)

REPO = RepoConfig(
    full_name="you/repo",
    role="own",
    label_map={"bug": "bug", "feature": "enhancement", "docs": "documentation"},
)

EXISTING_LABELS = {"bug", "enhancement", "documentation", "good-first-issue"}


def issue(number=1, title="Something broke", body="It crashed."):
    return {"number": number, "title": title, "body": body}


# -- priority escalation (§7.2) --------------------------------------------------


def test_escalate_priority_forces_p1_for_security_bug():
    text = "The app has a security vulnerability in the login flow"
    assert escalate_priority("bug", "p3", text) == "p1"


def test_escalate_priority_never_downgrades_a_worse_priority():
    text = "There is a crash on start on Windows"
    assert escalate_priority("bug", "p0", text) == "p0"  # already more urgent than p1


def test_escalate_priority_untouched_for_non_bug():
    text = "This is a security feature request"
    assert escalate_priority("feature", "p3", text) == "p3"


def test_escalate_priority_untouched_without_keyword_match():
    assert escalate_priority("bug", "p3", "the button is the wrong color") == "p3"


# -- label allowlisting (§7.2, §8.2) ---------------------------------------------


def test_filter_labels_drops_non_allowlisted():
    result = filter_labels(["bug", "made-up-label", "enhancement"], EXISTING_LABELS)
    assert result == ["bug", "enhancement"]


def test_filter_labels_caps_at_three():
    raw_labels = ["bug", "enhancement", "documentation", "good-first-issue"]
    result = filter_labels(raw_labels, EXISTING_LABELS)
    assert len(result) == 3


def test_filter_labels_ignores_non_string_entries():
    result = filter_labels(["bug", 123, None, {"name": "enhancement"}], EXISTING_LABELS)
    assert result == ["bug"]


# -- duplicate confirmation (§5.3, §8.5) -----------------------------------------


def test_filter_duplicates_only_keeps_offered_and_confirmed():
    candidates = [{"number": 31, "title": "Old bug", "state": "closed"}]
    raw = [{"number": 31, "duplicate_likely": True}]
    result = filter_duplicates(raw, candidates)
    assert result == [
        {"number": 31, "title": "Old bug", "state": "closed", "duplicate_likely": True}
    ]


def test_filter_duplicates_drops_unconfirmed():
    candidates = [{"number": 31, "title": "Old bug"}]
    raw = [{"number": 31, "duplicate_likely": False}]
    assert filter_duplicates(raw, candidates) == []


def test_filter_duplicates_drops_numbers_not_offered_as_candidates():
    """The model can't claim a duplicate against an issue it was never shown (§8.5)."""
    candidates = [{"number": 31, "title": "Old bug"}]
    raw = [{"number": 999, "duplicate_likely": True}]
    assert filter_duplicates(raw, candidates) == []


# -- number guard (narrow, triage-specific) --------------------------------------


def test_contains_number_support_true_when_numbers_match():
    assert contains_number_support("fails on version 3.2", "Using version 3.2 here") is True


def test_contains_number_support_false_for_invented_number():
    assert contains_number_support("affects 500 users", "just one user reported this") is False


# -- postprocess: the full pipeline, with an adversarial/injection-style raw output --


def test_postprocess_strips_disallowed_labels_and_inflated_duplicates():
    raw = {
        "classification": "bug",
        "priority": "p0",
        "confidence": "high",
        "suggested_labels": ["bug", "security", "p0-priority"],  # not real labels
        "missing_info": ["steps to reproduce", "made up field"],
        "summary": "The app crashes",
        "duplicates": [{"number": 999, "duplicate_likely": True}],  # not an offered candidate
        "first_response": "Thanks for the report!",
    }
    result = postprocess(
        raw, issue(), candidates=[], existing_labels=EXISTING_LABELS, label_map=REPO.label_map
    )
    assert result.suggested_labels == ["bug"]
    assert result.missing_info == ["steps to reproduce"]
    assert result.duplicates == []
    assert result.priority == "p0"  # model's own priority is otherwise respected


def test_postprocess_invalid_enum_values_fall_back_safely():
    raw = {"classification": "haxx0r", "priority": "p99", "confidence": "extremely high"}
    result = postprocess(raw, issue(), [], EXISTING_LABELS, REPO.label_map)
    assert result.classification == "other"
    assert result.priority == "p2"
    assert result.confidence == "low"


def test_postprocess_drops_summary_with_invented_number():
    raw = {
        "classification": "bug",
        "priority": "p2",
        "confidence": "high",
        "summary": "Reported by 500 different users",
    }
    result = postprocess(raw, issue(body="Just me."), [], EXISTING_LABELS, REPO.label_map)
    assert result.summary is None


def test_postprocess_adds_mapped_label_from_classification():
    raw = {
        "classification": "bug",
        "priority": "p2",
        "confidence": "medium",
        "suggested_labels": [],
    }
    result = postprocess(raw, issue(), [], EXISTING_LABELS, REPO.label_map)
    assert "bug" in result.suggested_labels


def test_postprocess_non_dict_fields_are_safely_ignored():
    raw = {
        "classification": "bug",
        "priority": "p2",
        "confidence": "medium",
        "suggested_labels": "bug",  # a raw string, not a list -- must not be iterated char by char
    }
    result = postprocess(raw, issue(), [], EXISTING_LABELS, REPO.label_map)
    assert result.suggested_labels == ["bug"]  # only the mapped label, from classification


# -- content hashing / caching ----------------------------------------------------


def test_content_hash_stable_for_same_content():
    assert content_hash(issue()) == content_hash(issue())


def test_content_hash_changes_with_body():
    assert content_hash(issue(body="a")) != content_hash(issue(body="b"))


def test_triage_issue_uses_cache_without_calling_classify_fn():
    an_issue = issue()
    cache_entry = {
        "hash": content_hash(an_issue),
        "prompt_version": PROMPT_VERSION,
        "result": {
            "number": 1,
            "classification": "bug",
            "priority": "p2",
            "confidence": "high",
            "suggested_labels": ["bug"],
            "missing_info": [],
            "summary": "cached summary",
            "first_response": None,
            "duplicates": [],
        },
    }

    def classify_fn(_input: TriageInput):
        raise AssertionError("classify_fn should not be called on a cache hit")

    result, cached = triage_issue(an_issue, [], EXISTING_LABELS, REPO, cache_entry, classify_fn)
    assert cached is True
    assert result.summary == "cached summary"


def test_triage_issue_calls_classify_fn_on_stale_hash():
    an_issue = issue()
    cache_entry = {"hash": "stale-hash", "prompt_version": PROMPT_VERSION, "result": {}}
    calls = []

    def classify_fn(triage_input: TriageInput):
        calls.append(triage_input)
        return {"classification": "bug", "priority": "p2", "confidence": "medium"}

    result, cached = triage_issue(an_issue, [], EXISTING_LABELS, REPO, cache_entry, classify_fn)
    assert cached is False
    assert len(calls) == 1
    assert result.classification == "bug"


def test_triage_issue_returns_none_with_no_cache_and_no_classify_fn():
    """No ANTHROPIC_API_KEY tonight -- see DECISIONS.md. Nothing to publish yet."""
    result, cached = triage_issue(issue(), [], EXISTING_LABELS, REPO, None, None)
    assert result is None
    assert cached is False


# -- per-run cap (§7.1) ------------------------------------------------------------


def test_triage_repo_respects_max_per_run_cap():
    issues = [issue(number=n) for n in range(1, 6)]
    calls = []

    def classify_fn(triage_input: TriageInput):
        calls.append(triage_input.issue["number"])
        return {"classification": "bug", "priority": "p2", "confidence": "medium"}

    results = triage_repo(issues, {}, EXISTING_LABELS, REPO, {}, classify_fn, max_per_run=2)
    assert len(calls) == 2
    triaged = [r for r, _cached in results if r is not None]
    assert len(triaged) == 2
    skipped = [r for r, _cached in results if r is None]
    assert len(skipped) == 3


def test_triage_repo_cache_hits_dont_count_against_cap():
    issues = [issue(number=1), issue(number=2)]
    cache = {
        "1": {
            "hash": content_hash(issue(number=1)),
            "prompt_version": PROMPT_VERSION,
            "result": {
                "number": 1,
                "classification": "bug",
                "priority": "p2",
                "confidence": "high",
                "suggested_labels": [],
                "missing_info": [],
                "summary": None,
                "first_response": None,
                "duplicates": [],
            },
        }
    }
    calls = []

    def classify_fn(triage_input: TriageInput):
        calls.append(triage_input.issue["number"])
        return {"classification": "bug", "priority": "p2", "confidence": "medium"}

    results = triage_repo(issues, {}, EXISTING_LABELS, REPO, cache, classify_fn, max_per_run=1)
    assert calls == [2]
    assert all(r is not None for r, _cached in results)
