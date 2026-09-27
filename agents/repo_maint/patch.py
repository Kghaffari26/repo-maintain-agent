"""Parse and apply unified diffs in memory, for the fix proposer (SPEC_REPO_MAINT.md §6.1).

The model's ``propose_patch`` diff is only ever *applied to strings* here: nothing
touches a working tree or GitHub. A patch that applies becomes the proposal's new
file contents, which are what a human-approved draft PR would commit.

Model-written diffs are often slightly off, so hunks are located by their context
and removed lines (nearest match to the header's line number, in order), not by
trusting the ``@@`` counts; blank context lines that lost their leading space are
accepted. Everything else is strict: the old lines must match exactly (trailing
whitespace aside).

Safety limits (``PatchLimits``): at most 3 files and 80 changed lines, no deletions
or renames, only relative paths inside the repo, and nothing under ``.github/``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FORBIDDEN_PREFIXES = (".github/", ".git/")
#: Marks a blank line inside a hunk that lost its leading space: context if more hunk
#: lines follow, a separator if it trails the hunk.
_BLANK = "\x00"


class PatchError(ValueError):
    """The diff doesn't parse, breaks a limit, or doesn't apply."""


@dataclass(frozen=True)
class PatchLimits:
    max_files: int = 3
    max_changed_lines: int = 80


@dataclass
class Hunk:
    old_start: int
    lines: list[str] = field(default_factory=list)  # each starts with " ", "-" or "+"

    @property
    def old_lines(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " -"]

    @property
    def new_lines(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " +"]


@dataclass
class FilePatch:
    path: str
    is_new: bool
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line[0] == "+")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line[0] == "-")


@dataclass
class AppliedPatch:
    files: dict[str, str]  # path -> new content
    added: int
    removed: int

    @property
    def paths(self) -> list[str]:
        return sorted(self.files)


def _clean_path(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def check_path(path: str) -> None:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(p in ("", ".", "..") for p in parts)
        or path.startswith(_FORBIDDEN_PREFIXES)
    ):
        raise PatchError(f"path not allowed: {path!r}")


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Every file section of a unified diff (``---``/``+++`` headers, ``@@`` hunks)."""
    lines = text.replace("\r\n", "\n").split("\n")
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _clean_path(line[4:])
            new = _clean_path(lines[i + 1][4:])
            if new is None:
                raise PatchError(f"deleting files is not allowed ({old})")
            if old is not None and old != new:
                raise PatchError(f"renames are not allowed ({old} -> {new})")
            check_path(new)
            current = FilePatch(path=new, is_new=old is None)
            patches.append(current)
            hunk = None
            i += 2
            continue
        match = _HUNK_RE.match(line)
        if match:
            if current is None:
                raise PatchError("hunk before any ---/+++ file header")
            hunk = Hunk(old_start=int(match.group(1)))
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line == "" and i + 1 < len(lines):
            hunk.lines.append(_BLANK)
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        # anything else (diff --git, index, prose) is ignored between sections
        i += 1
    if not patches:
        raise PatchError("no file sections found (expected ---/+++ headers)")
    for fp in patches:
        for h in fp.hunks:
            while h.lines and h.lines[-1] == _BLANK:
                h.lines.pop()
            h.lines = [" " if line == _BLANK else line for line in h.lines]
        if not fp.hunks or not any(h.lines for h in fp.hunks):
            raise PatchError(f"{fp.path}: no hunks")
        if not any(line[0] in "+-" for h in fp.hunks for line in h.lines):
            raise PatchError(f"{fp.path}: no changed lines")
    return patches


def _find(haystack: list[str], needle: list[str], start: int, hint: int) -> int | None:
    """Index of ``needle`` in ``haystack[start:]`` nearest to ``hint``, comparing
    lines with trailing whitespace stripped."""
    if not needle:
        return max(start, min(hint, len(haystack)))
    target = [n.rstrip() for n in needle]
    best: int | None = None
    for pos in range(start, len(haystack) - len(needle) + 1):
        if [h.rstrip() for h in haystack[pos : pos + len(needle)]] == target and (
            best is None or abs(pos - hint) < abs(best - hint)
        ):
            best = pos
    return best


def apply_file_patch(original: str | None, fp: FilePatch) -> str:
    if fp.is_new:
        if original is not None:
            raise PatchError(f"{fp.path}: patch creates a file that already exists")
        new = [line for h in fp.hunks for line in h.new_lines]
        return "\n".join(new) + "\n"
    if original is None:
        raise PatchError(f"{fp.path}: no such file")
    trailing_newline = original.endswith("\n")
    lines = original.split("\n")
    if trailing_newline:
        lines.pop()
    out: list[str] = []
    cursor = 0
    for n, h in enumerate(fp.hunks, 1):
        pos = _find(lines, h.old_lines, cursor, max(h.old_start - 1, 0))
        if pos is None:
            raise PatchError(
                f"{fp.path}: hunk {n} does not apply (its context/removed lines were not"
                " found; copy them exactly from read_file)"
            )
        out += lines[cursor:pos] + h.new_lines
        cursor = pos + len(h.old_lines)
    out += lines[cursor:]
    return "\n".join(out) + ("\n" if trailing_newline else "")


def apply_patch(
    diff: str,
    read: Callable[[str], str | None],
    limits: PatchLimits | None = None,
) -> AppliedPatch:
    """Apply ``diff`` to files obtained through ``read(path)`` (None: no such file)."""
    limits = limits or PatchLimits()
    patches = parse_unified_diff(diff)
    paths = [fp.path for fp in patches]
    if len(set(paths)) != len(paths):
        raise PatchError("each file may appear only once in the diff")
    if len(patches) > limits.max_files:
        raise PatchError(f"patch touches {len(patches)} files; the limit is {limits.max_files}")
    added = sum(fp.added for fp in patches)
    removed = sum(fp.removed for fp in patches)
    if added + removed > limits.max_changed_lines:
        raise PatchError(
            f"patch changes {added + removed} lines; the limit is {limits.max_changed_lines}"
        )
    files = {fp.path: apply_file_patch(read(fp.path), fp) for fp in patches}
    return AppliedPatch(files=files, added=added, removed=removed)
