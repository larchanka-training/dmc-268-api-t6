"""Provenance of a context fragment: role, revision, path and source line ranges."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.modules.reviews.application.context_provenance import (
    LineRange,
    SourceRef,
    SourceRole,
    hunk_source_ref,
)
from app.modules.reviews.application.diff_map import (
    DiffMap,
    DiffSide,
    VcsProviderName,
    build_diff_map,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest

_HEAD = "a" * 40
_BLOB = "d" * 40
_REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000001")


def _diff_map(file: VcsFile) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(17, "octo/repo", 7),
        head_sha=_HEAD,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="Fix parser",
            description=None,
            author="octo",
            source_branch="feature",
            target_branch="main",
            labels=(),
            files_changed=1,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        (file,),
        provider=VcsProviderName.GITHUB,
        repository_id=_REPOSITORY_ID,
        diff_old_sha="c" * 40,
    )


def _file(*, patch: str | None, additions: int, deletions: int, renamed: bool = False) -> VcsFile:
    return VcsFile(
        filename="src/new.py",
        status="renamed" if renamed else "modified",
        blob_sha=_BLOB,
        previous_filename="generated/old.py" if renamed else None,
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
        patch=patch,
    )


def test_hunk_provenance_points_at_new_side_lines_of_the_head_revision() -> None:
    diff_map = _diff_map(_file(patch="@@ -3,2 +3,3 @@\n a\n+b\n c", additions=1, deletions=0))
    file = diff_map.files[0]

    assert hunk_source_ref(diff_map, file, file.hunks[0]) == SourceRef(
        role=SourceRole.DIFF,
        repository_id=_REPOSITORY_ID,
        revision=_HEAD,
        path="src/new.py",
        blob_sha=_BLOB,
        origin_paths=(),
        side=DiffSide.RIGHT,
        ranges=(LineRange(3, 5),),
        reason="changed_hunk",
    )


def test_renamed_file_keeps_its_earlier_path_as_origin() -> None:
    diff_map = _diff_map(_file(patch="@@ -1 +1 @@\n-a\n+b", additions=1, deletions=1, renamed=True))
    file = diff_map.files[0]

    assert hunk_source_ref(diff_map, file, file.hunks[0]).origin_paths == ("generated/old.py",)


def test_hunk_that_only_removes_lines_has_no_new_side_range() -> None:
    diff_map = _diff_map(_file(patch="@@ -4,2 +3,0 @@\n-a\n-b", additions=0, deletions=2))
    file = diff_map.files[0]

    assert hunk_source_ref(diff_map, file, file.hunks[0]).ranges == ()


def test_file_without_a_complete_diff_has_no_hunk_provenance() -> None:
    diff_map = _diff_map(_file(patch="@@ -1 +1 @@\n-a\n+b", additions=7, deletions=1))
    file = diff_map.files[0]

    with pytest.raises(ValueError, match="complete diff"):
        hunk_source_ref(diff_map, file, file.hunks[0])


def test_line_range_is_one_based_and_ordered() -> None:
    assert LineRange(1, 1).end_line == 1
    for start_line, end_line in ((0, 1), (3, 2)):
        with pytest.raises(ValueError, match="line range"):
            LineRange(start_line, end_line)


def test_source_roles_are_the_list_of_the_specification() -> None:
    assert {role.value for role in SourceRole} == {
        "policy_document",
        "service_defaults",
        "conventions",
        "pr_metadata",
        "diff",
        "source_excerpt",
        "ast_excerpt",
        "candidate",
        "previous_output",
    }


def test_deleted_file_hunk_points_at_old_lines_of_the_old_revision() -> None:
    removed = VcsFile(
        filename="src/gone.py",
        status="removed",
        blob_sha=None,
        previous_filename=None,
        additions=0,
        deletions=2,
        changes=2,
        patch="@@ -4,2 +0,0 @@\n-a\n-b",
    )
    diff_map = _diff_map(removed)
    file = diff_map.files[0]

    ref = hunk_source_ref(diff_map, file, file.hunks[0])

    assert (ref.side, ref.path, ref.revision) == (DiffSide.LEFT, "src/gone.py", "c" * 40)
    assert (ref.ranges, ref.blob_sha) == ((LineRange(4, 5),), None)
