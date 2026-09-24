"""Comment sanitization, applied to ``first_response`` before posting (§8.4).

This is the last line of defense before anything derived from untrusted
issue/model text reaches a real GitHub comment -- see §8.5. A comment
that trips a reject pattern is dropped entirely (only the fixed template
part of the §8.3 comment is posted), never partially redacted and sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agents.repo_maint.untriaged import TRIAGE_MARKER

ZERO_WIDTH_JOINER = "‍"
MAX_WORDS = 80
MAX_CHARS = 600

_MENTION_RE = re.compile(r"@(\w+)")
_URL_RE = re.compile(r"https?://\S+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

_SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{12,}"),
    re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
]
_REJECT_PHRASES = [TRIAGE_MARKER, "ignore previous", "system prompt"]


@dataclass
class SanitizeResult:
    text: str | None  # None when rejected outright
    rejected: bool
    reasons: list[str]


def neutralize_mentions(text: str) -> str:
    """Insert a zero-width joiner after ``@`` so nobody gets pinged (§8.4)."""
    return _MENTION_RE.sub(lambda m: f"@{ZERO_WIDTH_JOINER}{m.group(1)}", text)


def strip_urls_except_repo(text: str, owner: str, repo: str) -> str:
    """Remove URLs, except links to this repo on github.com (§8.4)."""
    allowed_prefix = f"https://github.com/{owner}/{repo}"

    def _replace(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(").,")
        if url.startswith(allowed_prefix) or url.startswith(
            allowed_prefix.replace("https://", "http://")
        ):
            return match.group(0)
        return ""

    return _URL_RE.sub(_replace, text)


def strip_html_and_images(text: str) -> str:
    """Remove HTML tags and markdown images (§8.4)."""
    text = _MD_IMAGE_RE.sub("", text)
    return _HTML_TAG_RE.sub("", text)


def collapse_length(text: str) -> str:
    """Collapse to 80 words or fewer and 600 characters or fewer (§8.4)."""
    words = text.split()
    if len(words) > MAX_WORDS:
        text = " ".join(words[:MAX_WORDS])
    return text[:MAX_CHARS]


def reject_reasons(text: str) -> list[str]:
    """Banned phrases or secret-like patterns that reject the whole comment (§8.4)."""
    reasons = []
    lower = text.lower()
    for phrase in _REJECT_PHRASES:
        if phrase.lower() in lower:
            reasons.append(f"contains banned phrase: {phrase!r}")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        reasons.append("looks like a secret or access token")
    return reasons


def sanitize_first_response(text: str, owner: str, repo: str) -> SanitizeResult:
    """The full §8.4 pipeline. Rejection is checked on the *raw* text first,
    since a token or an injected instruction shouldn't survive by being
    partially cleaned away."""
    reasons = reject_reasons(text)
    if reasons:
        return SanitizeResult(text=None, rejected=True, reasons=reasons)

    cleaned = strip_html_and_images(text)
    cleaned = strip_urls_except_repo(cleaned, owner, repo)
    cleaned = neutralize_mentions(cleaned)
    cleaned = collapse_length(cleaned)
    return SanitizeResult(text=cleaned, rejected=False, reasons=[])
