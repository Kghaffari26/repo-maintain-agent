"""Deterministic duplicate-issue candidates via TF-IDF (SPEC_REPO_MAINT.md §5.3).

This is deliberately deterministic and LLM-free: it only *proposes*
candidates. Confirming a candidate as an actual duplicate is the triage
LLM call's job (``triage.py``), which returns ``duplicate_likely`` per
candidate.
"""

from __future__ import annotations

import re
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

DEFAULT_THRESHOLD = 0.45
MAX_CANDIDATES = 3
BODY_CHARS = 1000

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def make_document(issue: dict[str, Any]) -> str:
    """Title + first 1000 chars of body, lowercased, with fenced code blocks removed (§5.3)."""
    title = issue.get("title") or ""
    body = _CODE_BLOCK_RE.sub(" ", issue.get("body") or "")[:BODY_CHARS]
    return f"{title} {body}".lower()


class DuplicateIndex:
    """A TF-IDF index over a repo's duplicate-matching corpus, fit once per run."""

    def __init__(self, corpus_issues: list[dict[str, Any]]) -> None:
        self._issues = corpus_issues
        self._number_to_index = {issue["number"]: idx for idx, issue in enumerate(corpus_issues)}
        if corpus_issues:
            documents = [make_document(issue) for issue in corpus_issues]
            self._vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
            self._matrix = self._vectorizer.fit_transform(documents)
        else:
            self._vectorizer = None
            self._matrix = None

    def candidates(
        self,
        issue_number: int,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        top_k: int = MAX_CANDIDATES,
    ) -> list[tuple[dict[str, Any], float]]:
        """The top ``top_k`` other issues with cosine similarity >= ``threshold``."""
        if self._matrix is None:
            return []
        idx = self._number_to_index.get(issue_number)
        if idx is None:
            return []
        similarities = cosine_similarity(self._matrix[idx], self._matrix).flatten()
        scored = [
            (self._issues[other_idx], float(score))
            for other_idx, score in enumerate(similarities)
            if other_idx != idx and score >= threshold
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


def build_corpus(
    open_issues: list[dict[str, Any]], closed_issues_last_180d: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Open issues + issues closed in the last 180 days, PRs excluded, de-duplicated by number."""
    seen: dict[int, dict[str, Any]] = {}
    for issue in [*open_issues, *closed_issues_last_180d]:
        if "pull_request" in issue:
            continue
        seen[issue["number"]] = issue
    return list(seen.values())
