"""Tests for agents.repo_maint.patch (§6.1): parsing and applying model-written diffs."""

from __future__ import annotations

import pytest

from agents.repo_maint.patch import (
    PatchError,
    PatchLimits,
    apply_file_patch,
    apply_patch,
    check_path,
    parse_unified_diff,
)

ORIGINAL = "def paginate(items, page, per_page=10):\n    start = page * per_page\n    return items[start : start + per_page]\n"  # noqa: E501

DIFF = """--- a/pkg/pagination.py
+++ b/pkg/pagination.py
@@ -1,3 +1,3 @@
 def paginate(items, page, per_page=10):
-    start = page * per_page
+    start = (page - 1) * per_page
     return items[start : start + per_page]
"""


def test_applies_a_clean_diff():
    result = apply_patch(DIFF, {"pkg/pagination.py": ORIGINAL}.get)
    assert result.paths == ["pkg/pagination.py"]
    assert "(page - 1) * per_page" in result.files["pkg/pagination.py"]
    assert (result.added, result.removed) == (1, 1)


def test_wrong_hunk_line_numbers_are_tolerated():
    shifted = DIFF.replace("@@ -1,3 +1,3 @@", "@@ -40,7 +40,9 @@")
    result = apply_patch(shifted, lambda p: ORIGINAL)
    assert "(page - 1)" in result.files["pkg/pagination.py"]


def test_the_match_nearest_the_hunk_header_wins():
    original = "x = 1\ny = 2\nx = 1\ny = 2\n"
    diff = "--- a/f.py\n+++ b/f.py\n@@ -3,2 +3,2 @@\n x = 1\n-y = 2\n+y = 3\n"
    assert apply_patch(diff, lambda p: original).files["f.py"] == "x = 1\ny = 2\nx = 1\ny = 3\n"


def test_blank_context_lines_without_their_leading_space_are_accepted():
    original = "a = 1\n\nb = 2\n"
    diff = "--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,3 @@\n a = 1\n\n-b = 2\n+b = 3\n\n"
    assert apply_patch(diff, lambda p: original).files["f.py"] == "a = 1\n\nb = 3\n"


def test_multiple_hunks_and_trailing_newline_preserved():
    original = "a\nb\nc\nd\ne"
    diff = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n@@ -4,2 +4,2 @@\n d\n-e\n+E\n"
    assert apply_patch(diff, lambda p: original).files["f"] == "A\nb\nc\nd\nE"


def test_new_files_are_created():
    diff = "--- /dev/null\n+++ b/tests/test_x.py\n@@ -0,0 +1,2 @@\n+def test_x():\n+    pass\n"
    result = apply_patch(diff, lambda p: None)
    assert result.files["tests/test_x.py"] == "def test_x():\n    pass\n"


@pytest.mark.parametrize(
    ("diff", "message"),
    [
        ("just prose", "no file sections"),
        ("--- a/f\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n", "deleting"),
        ("--- a/f\n+++ b/g\n@@ -1 +1 @@\n-a\n+b\n", "renames"),
        ("--- a/f\n+++ b/f\n", "no hunks"),
        ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n a\n", "no changed lines"),
        (
            "--- a/.github/workflows/x.yml\n+++ b/.github/workflows/x.yml\n@@ -1 +1 @@\n-a\n+b\n",
            "not allowed",
        ),  # noqa: E501
        ("--- a/../etc/passwd\n+++ b/../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n", "not allowed"),
        ("@@ -1 +1 @@\n-a\n+b\n", "hunk before any"),
    ],
)
def test_rejects_unsafe_or_malformed_diffs(diff, message):
    with pytest.raises(PatchError, match=message):
        apply_patch(diff, lambda p: "a\n")


def test_rejects_hunks_whose_context_is_not_in_the_file():
    bad = DIFF.replace("    start = page * per_page", "    start = page*per_page")
    with pytest.raises(PatchError, match="hunk 1 does not apply"):
        apply_patch(bad, lambda p: ORIGINAL)


def test_rejects_missing_files_and_creating_existing_ones():
    with pytest.raises(PatchError, match="no such file"):
        apply_patch(DIFF, lambda p: None)
    new = "--- /dev/null\n+++ b/f.py\n@@ -0,0 +1 @@\n+x\n"
    with pytest.raises(PatchError, match="already exists"):
        apply_patch(new, lambda p: "x\n")


def test_limits_on_files_and_changed_lines():
    many = "".join(f"--- a/f{i}\n+++ b/f{i}\n@@ -1 +1 @@\n-a\n+b\n" for i in range(4))
    with pytest.raises(PatchError, match="4 files"):
        apply_patch(many, lambda p: "a\n")
    big = "--- a/f\n+++ b/f\n@@ -1 +1,81 @@\n-a\n" + "+b\n" * 80
    with pytest.raises(PatchError, match="81 lines"):
        apply_patch(big, lambda p: "a\n")
    assert apply_patch(big, lambda p: "a\n", PatchLimits(max_changed_lines=100)).added == 80
    twice = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n" * 2
    with pytest.raises(PatchError, match="only once"):
        apply_patch(twice, lambda p: "a\n")


def test_parse_and_apply_file_patch_directly():
    [fp] = parse_unified_diff(DIFF)
    assert fp.path == "pkg/pagination.py" and not fp.is_new
    assert (fp.added, fp.removed) == (1, 1)
    assert apply_file_patch(ORIGINAL, fp).count("\n") == 3


@pytest.mark.parametrize("path", ["", "/abs", "a/../b", "a//b", "a\\b", ".git/config"])
def test_check_path_rejects(path):
    with pytest.raises(PatchError):
        check_path(path)
