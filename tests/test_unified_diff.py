"""The shared walk over unified diff hunks and the two contracts built on it."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.modules.reviews.application.diff_map import DiffFile, VcsProviderName, build_diff_map
from app.modules.reviews.application.prompt_builder import DiffLine, PullRequestMeta
from app.modules.reviews.application.unified_diff import (
    Hunk,
    HunkRow,
    UnifiedDiffError,
    parse_hunks,
    split_lf,
)
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
    parse_file_patch,
)

_PATCH = (
    "@@ -3,3 +3,4 @@ def f():\n context\n-old\n+new\n+extra\n tail\n"
    "\\ No newline at end of file\n@@ -10 +20 @@\n-gone\n+added"
)


def test_hunks_carry_both_line_numbers_and_skip_the_no_newline_marker() -> None:
    assert parse_hunks(split_lf(_PATCH)) == (
        Hunk(
            3,
            3,
            3,
            4,
            (
                HunkRow("context", 3, 3, "context"),
                HunkRow("removed", 4, None, "old"),
                HunkRow("added", None, 4, "new"),
                HunkRow("added", None, 5, "extra"),
                HunkRow("context", 5, 6, "tail"),
            ),
        ),
        Hunk(
            10,
            1,
            20,
            1,
            (HunkRow("removed", 10, None, "gone"), HunkRow("added", None, 20, "added")),
        ),
    )


def test_absent_count_is_one_line_and_zero_is_an_empty_side() -> None:
    (added,) = parse_hunks(["@@ -0,0 +1,2 @@", "+a", "+b"])
    (removed,) = parse_hunks(["@@ -4 +3,0 @@", "-a"])

    assert (added.old_count, added.new_count) == (0, 2)
    assert (removed.old_count, removed.new_count) == (1, 0)


def test_patch_that_disagrees_with_its_headers_is_rejected() -> None:
    cases = {
        "no hunk header": ["+new"],
        "unknown line": ["@@ -1 +1 @@", "-old", "+new", "?what"],
        "exceeded": ["@@ -1 +1 @@", "-old", "+new", "+extra"],
        "ended before": ["@@ -1,2 +1,2 @@", "-old", "@@ -5 +5 @@", "-a", "+b"],
        "incomplete": ["@@ -1,2 +1,2 @@", "-old", "+new"],
    }
    for message, lines in cases.items():
        with pytest.raises(UnifiedDiffError, match=message):
            parse_hunks(lines)
    with pytest.raises(UnifiedDiffError, match="incomplete"):
        parse_hunks([])
    # The existing callers catch a plain ValueError.
    assert issubclass(UnifiedDiffError, ValueError)


def test_split_keeps_other_separators_and_a_carriage_return() -> None:
    assert split_lf("") == []
    assert split_lf("a\nb\n") == ["a", "b"]
    assert split_lf("a\r\nb c") == ["a\r", "b c"]


def test_prompt_input_and_diff_map_read_the_same_hunks() -> None:
    changed = parse_file_patch("src/app.py", "modified", _PATCH)

    assert changed.lines == (
        DiffLine(3, "context", "context"),
        DiffLine(4, "removed", "old"),
        DiffLine(4, "added", "new"),
        DiffLine(5, "added", "extra"),
        DiffLine(6, "context", "tail"),
        DiffLine(10, "removed", "gone"),
        DiffLine(20, "added", "added"),
    )
    rows = [row for hunk in _diff_file(_PATCH, 3, 2).hunks for row in hunk.rows]
    assert [(row.kind.value, row.text) for row in rows] == [
        (line.type, line.content) for line in changed.lines
    ]


def test_each_contract_keeps_its_own_carriage_return_rule() -> None:
    patch = "@@ -1 +1 @@\n-old\r\n+new\r"

    # The prompt input has always dropped a trailing CR from every line.
    assert [line.content for line in parse_file_patch("a.py", "modified", patch).lines] == [
        "old",
        "new",
    ]
    # The diff map keeps the CR of a source line: it is evidence, not framing.
    assert [row.text for row in _diff_file(patch, 1, 1).hunks[0].rows] == ["old\r", "new\r"]


def _diff_file(patch: str, additions: int, deletions: int) -> DiffFile:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(17, "octo/repo", 7),
        head_sha="a" * 40,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="t",
            description=None,
            author="octo",
            source_branch="f",
            target_branch="main",
            labels=(),
            files_changed=1,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    file = VcsFile(
        "src/app.py", "modified", "d" * 40, None, additions, deletions, additions + deletions, patch
    )
    return build_diff_map(
        pull_request,
        (file,),
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha=None,
    ).files[0]
