"""Inline anchor of a finding: one file, the new side, one complete hunk."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from app.modules.reviews.application.diff_anchor import Anchor, AnchorRejection, resolve_anchor
from app.modules.reviews.application.diff_map import (
    DiffMap,
    DiffSide,
    VcsProviderName,
    build_diff_map,
)
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest

_PATCH = "@@ -3,3 +3,4 @@\n context\n-old\n+new\n+extra\n tail\n@@ -10 +20 @@\n-gone\n+added"


def _file(
    filename: str,
    *,
    status: FileStatus = "modified",
    patch: str | None = _PATCH,
    additions: int = 3,
    deletions: int = 2,
    previous_filename: str | None = None,
) -> VcsFile:
    return VcsFile(
        filename=filename,
        status=status,
        blob_sha=None if status == "removed" else "d" * 40,
        previous_filename=previous_filename,
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
        patch=patch,
    )


def _diff_map(*files: VcsFile) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(17, "octo/repo", 7),
        head_sha="a" * 40,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="Fix parser",
            description=None,
            author="octo",
            source_branch="feature",
            target_branch="main",
            labels=(),
            files_changed=len(files),
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        files,
        provider=VcsProviderName.GITHUB,
        repository_id=UUID("00000000-0000-0000-0000-000000000001"),
        diff_old_sha="c" * 40,
    )


def test_single_line_and_range_inside_one_hunk_are_anchored_on_the_new_side() -> None:
    diff_map = _diff_map(_file("src/app.py"))
    file = diff_map.files[0]

    assert resolve_anchor(diff_map, path="src/app.py", start_line=None, line=4) == Anchor(
        file.file_id, DiffSide.RIGHT, None, 4, file.hunks[0].hunk_id
    )
    assert resolve_anchor(diff_map, path="src/app.py", start_line=3, line=6) == Anchor(
        file.file_id, DiffSide.RIGHT, 3, 6, file.hunks[0].hunk_id
    )
    assert resolve_anchor(diff_map, path="src/app.py", start_line=None, line=20) == Anchor(
        file.file_id, DiffSide.RIGHT, None, 20, file.hunks[1].hunk_id
    )


def test_line_outside_the_diff_is_absent_and_never_moved_to_a_neighbour() -> None:
    diff_map = _diff_map(_file("src/app.py"))

    assert (
        resolve_anchor(diff_map, path="src/app.py", start_line=None, line=7)
        is AnchorRejection.LINE_ABSENT
    )
    assert (
        resolve_anchor(diff_map, path="src/app.py", start_line=2, line=4)
        is AnchorRejection.LINE_ABSENT
    )


def test_removed_line_number_is_not_an_anchor_on_the_new_side() -> None:
    # Old line 10 was removed; new line 10 is not part of any hunk.
    diff_map = _diff_map(_file("src/app.py"))

    assert (
        resolve_anchor(diff_map, path="src/app.py", start_line=None, line=10)
        is AnchorRejection.LINE_ABSENT
    )


def test_range_across_two_hunks_is_rejected() -> None:
    diff_map = _diff_map(_file("src/app.py"))

    assert (
        resolve_anchor(diff_map, path="src/app.py", start_line=6, line=20)
        is AnchorRejection.CROSS_HUNK_RANGE
    )


def test_range_with_a_line_missing_from_the_hunk_is_a_gap() -> None:
    diff_map = _diff_map(_file("src/app.py"))
    file = diff_map.files[0]
    hunk = file.hunks[0]
    holed = replace(hunk, rows=tuple(row for row in hunk.rows if row.new_line != 5))
    diff_map = replace(diff_map, files=(replace(file, hunks=(holed, file.hunks[1])),))

    assert (
        resolve_anchor(diff_map, path="src/app.py", start_line=3, line=6)
        is AnchorRejection.RANGE_GAP
    )


def test_unknown_path_and_the_old_name_of_a_rename_are_not_anchors() -> None:
    diff_map = _diff_map(_file("src/new.py", status="renamed", previous_filename="src/old.py"))

    assert (
        resolve_anchor(diff_map, path="src/other.py", start_line=None, line=4)
        is AnchorRejection.UNKNOWN_PATH
    )
    assert (
        resolve_anchor(diff_map, path="src/old.py", start_line=None, line=4)
        is AnchorRejection.UNKNOWN_PATH
    )
    assert isinstance(resolve_anchor(diff_map, path="src/new.py", start_line=None, line=4), Anchor)


def test_valid_old_name_of_a_rename_with_a_rejected_new_name_is_not_a_deleted_file() -> None:
    diff_map = _diff_map(_file("bad\\new.py", status="renamed", previous_filename="src/old.py"))

    assert (
        resolve_anchor(diff_map, path="src/old.py", start_line=None, line=4)
        is AnchorRejection.UNKNOWN_PATH
    )
    assert (
        resolve_anchor(diff_map, path="bad\\new.py", start_line=None, line=4)
        is AnchorRejection.INCOMPLETE_DIFF
    )


def test_deleted_file_exists_only_on_the_other_side() -> None:
    diff_map = _diff_map(
        _file("gone.py", status="removed", patch="@@ -1 +0,0 @@\n-one", additions=0, deletions=1)
    )

    assert (
        resolve_anchor(diff_map, path="gone.py", start_line=None, line=1)
        is AnchorRejection.WRONG_SIDE
    )


def test_incomplete_or_unusable_file_gives_no_anchor() -> None:
    diff_map = _diff_map(
        _file("missing.py", patch=None),
        _file("truncated.py", additions=9),
        _file("bad\\path.py"),
        _file(
            "moved.py",
            status="renamed",
            previous_filename="was.py",
            patch=None,
            additions=0,
            deletions=0,
        ),
    )

    def reject(path: str) -> object:
        return resolve_anchor(diff_map, path=path, start_line=None, line=4)

    assert reject("missing.py") is AnchorRejection.INCOMPLETE_DIFF
    assert reject("truncated.py") is AnchorRejection.INCOMPLETE_DIFF
    assert reject("bad\\path.py") is AnchorRejection.INCOMPLETE_DIFF
    assert reject("moved.py") is AnchorRejection.LINE_ABSENT


def test_file_with_a_c1_control_character_in_its_name_gives_no_inline_anchor() -> None:
    diff_map = _diff_map(_file("src/a\x85b.py"))

    assert (
        resolve_anchor(diff_map, path="src/a\x85b.py", start_line=None, line=4)
        is AnchorRejection.INCOMPLETE_DIFF
    )


def test_lines_outside_the_finding_contract_are_a_caller_error() -> None:
    diff_map = _diff_map(_file("src/app.py"))

    for start_line, line in ((None, 0), (4, 4), (5, 4), (0, 4)):
        with pytest.raises(ValueError, match="start_line < line"):
            resolve_anchor(diff_map, path="src/app.py", start_line=start_line, line=line)


def test_rejection_reasons_are_the_list_of_the_specification() -> None:
    assert {reason.value for reason in AnchorRejection} == {
        "unknown_path",
        "wrong_side",
        "line_absent",
        "cross_hunk_range",
        "range_gap",
        "anchor_not_visible",
        "ignored_file",
        "incomplete_diff",
        "unsupported_provider_anchor",
    }
