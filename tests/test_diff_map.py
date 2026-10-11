"""DiffMap/v1: provider facts of one pull request diff, both sides of every hunk."""

from __future__ import annotations

from uuid import UUID

from app.modules.reviews.application.diff_map import (
    ChangeType,
    DiffMap,
    DiffOmissionReason,
    DiffRow,
    PatchState,
    RowKind,
    VcsProviderName,
    build_diff_map,
    is_supported_path,
)
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest

_HEAD = "a" * 40
_BASE = "b" * 40
_MERGE_BASE = "c" * 40
_BLOB = "d" * 40
_REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000001")


def _pull_request(*, files_changed: int = 1, head_sha: str = _HEAD) -> VcsPullRequest:
    return VcsPullRequest(
        locator=PullRequestLocator(17, "octo/repo", 7),
        head_sha=head_sha,
        base_sha=_BASE,
        meta=PullRequestMeta(
            title="Fix parser",
            description=None,
            author="octo",
            source_branch="feature",
            target_branch="main",
            labels=(),
            files_changed=files_changed,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )


def _file(
    filename: str = "src/app.py",
    *,
    status: FileStatus = "modified",
    patch: str | None = "@@ -1 +1 @@\n-old\n+new",
    additions: int = 1,
    deletions: int = 1,
    previous_filename: str | None = None,
) -> VcsFile:
    return VcsFile(
        filename=filename,
        status=status,
        blob_sha=None if status == "removed" else _BLOB,
        previous_filename=previous_filename,
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
        patch=patch,
    )


def _build(*files: VcsFile, files_changed: int | None = None, head_sha: str = _HEAD) -> DiffMap:
    return build_diff_map(
        _pull_request(
            files_changed=len(files) if files_changed is None else files_changed,
            head_sha=head_sha,
        ),
        files,
        provider=VcsProviderName.GITHUB,
        repository_id=_REPOSITORY_ID,
        diff_old_sha=_MERGE_BASE,
    )


def test_hunk_rows_carry_the_line_number_of_each_side() -> None:
    patch = (
        "@@ -3,3 +3,4 @@ def f():\n context\n-old\n+new\n+extra\n tail\n"
        "@@ -10 +20 @@\n-gone\n+added"
    )
    diff_map = _build(_file(patch=patch, additions=3, deletions=2))

    file = diff_map.files[0]
    assert file.patch_state is PatchState.COMPLETE
    assert [(h.old_start, h.old_count, h.new_start, h.new_count) for h in file.hunks] == [
        (3, 3, 3, 4),
        (10, 1, 20, 1),
    ]
    assert file.hunks[0].rows == (
        DiffRow(RowKind.CONTEXT, 3, 3, "context"),
        DiffRow(RowKind.REMOVED, 4, None, "old"),
        DiffRow(RowKind.ADDED, None, 4, "new"),
        DiffRow(RowKind.ADDED, None, 5, "extra"),
        DiffRow(RowKind.CONTEXT, 5, 6, "tail"),
    )
    assert file.hunks[1].rows == (
        DiffRow(RowKind.REMOVED, 10, None, "gone"),
        DiffRow(RowKind.ADDED, None, 20, "added"),
    )
    assert file.hunks[0].hunk_id != file.hunks[1].hunk_id


def test_map_keeps_both_revisions_apart_from_the_policy_base() -> None:
    diff_map = _build(_file())

    assert diff_map.schema_version == 1
    assert diff_map.provider is VcsProviderName.GITHUB
    assert diff_map.repository_id == _REPOSITORY_ID
    assert diff_map.code_change_number == 7
    assert diff_map.policy_base_sha == _BASE
    assert diff_map.head_sha == _HEAD
    assert diff_map.diff_old_sha == _MERGE_BASE
    assert diff_map.file_list_complete is True
    assert diff_map.incomplete_reasons == ()


def test_missing_side_of_an_added_or_deleted_file_is_absent() -> None:
    added = "@@ -0,0 +1,2 @@\n+one\n+two"
    removed = "@@ -1,2 +0,0 @@\n-one\n-two"
    diff_map = _build(
        _file("new.py", status="added", patch=added, additions=2, deletions=0),
        _file("gone.py", status="removed", patch=removed, additions=0, deletions=2),
    )

    new, gone = diff_map.files
    assert (new.change_type, new.old_path, new.new_path) == (ChangeType.ADDED, None, "new.py")
    assert (gone.change_type, gone.old_path, gone.new_path) == (ChangeType.DELETED, "gone.py", None)
    assert gone.blob_sha is None
    assert [row.new_line for row in new.hunks[0].rows] == [1, 2]
    assert [row.old_line for row in gone.hunks[0].rows] == [1, 2]


def test_rename_keeps_both_paths_and_a_pure_rename_is_metadata_only() -> None:
    diff_map = _build(
        _file(
            "src/new.py",
            status="renamed",
            previous_filename="src/old.py",
            patch=None,
            additions=0,
            deletions=0,
        )
    )

    file = diff_map.files[0]
    assert (file.change_type, file.old_path, file.new_path) == (
        ChangeType.RENAMED,
        "src/old.py",
        "src/new.py",
    )
    assert file.patch_state is PatchState.METADATA_ONLY
    assert file.hunks == ()
    assert file.anchorable is False


def test_copy_without_its_source_path_has_an_unresolved_identity() -> None:
    file = _build(_file("src/copy.py", status="copied")).files[0]

    assert file.change_type is ChangeType.COPIED
    assert file.omission_reason is DiffOmissionReason.IDENTITY_UNRESOLVED
    assert file.hunks == ()


def test_file_id_separates_a_copy_from_its_source() -> None:
    diff_map = _build(
        _file("src/a.py"),
        _file("src/b.py", status="copied", previous_filename="src/a.py"),
    )

    source, copy = diff_map.files
    assert source.file_id != copy.file_id
    assert diff_map.file_by_id(copy.file_id) is copy
    assert diff_map.file_by_id("missing") is None


def test_patch_states_distinguish_missing_binary_combined_and_malformed() -> None:
    diff_map = _build(
        _file("missing.py", patch=None),
        _file("image.bin", patch="@@ -1 +1 @@\n-a\x00\n+b"),
        _file("merge.py", patch="@@@ -1,2 -1,2 +1,3 @@@\n  a\n++b"),
        _file("no_header.py", patch="+new"),
        _file("short.py", patch="@@ -1,2 +1,2 @@\n-old\n+new"),
        _file("long.py", patch="@@ -1 +1 @@\n-old\n+new\n+extra"),
        _file("prefix.py", patch="@@ -1 +1 @@\n-old\n+new\n?what"),
        _file("zero.py", patch="@@ -0 +1 @@\n-old\n+new"),
        _file(
            "overlap.py", patch="@@ -1 +1 @@\n-a\n+b\n@@ -1 +1 @@\n-a\n+b", additions=2, deletions=2
        ),
    )

    assert [file.patch_state for file in diff_map.files] == [
        PatchState.MISSING,
        PatchState.BINARY,
        PatchState.UNSUPPORTED,
        PatchState.MALFORMED,
        PatchState.MALFORMED,
        PatchState.MALFORMED,
        PatchState.MALFORMED,
        PatchState.MALFORMED,
        PatchState.MALFORMED,
    ]
    assert all(file.hunks == () and not file.anchorable for file in diff_map.files)


def test_patch_shorter_than_the_provider_statistics_is_truncated() -> None:
    file = _build(_file(additions=5, deletions=1)).files[0]

    assert file.patch_state is PatchState.TRUNCATED
    assert file.anchorable is False


def test_no_newline_marker_is_not_a_row_and_crlf_framing_is_removed() -> None:
    patch = "@@ -1 +1 @@\r\n-old\r\n\\ No newline at end of file\r\n+new\r\n"
    file = _build(_file(patch=patch)).files[0]

    assert file.patch_state is PatchState.COMPLETE
    assert file.hunks[0].rows == (
        DiffRow(RowKind.REMOVED, 1, None, "old"),
        DiffRow(RowKind.ADDED, None, 1, "new"),
    )


def test_unsupported_path_keeps_the_file_in_the_map_without_its_content() -> None:
    diff_map = _build(_file("src\\app.py"), _file("src/ok.py"))

    bad, good = diff_map.files
    assert bad.new_path == "src\\app.py"
    assert bad.omission_reason is DiffOmissionReason.UNSUPPORTED_PATH
    assert bad.hunks == ()
    assert bad.anchorable is False
    assert good.omission_reason is None
    assert good.anchorable is True


def test_path_contract_of_the_specification() -> None:
    assert is_supported_path("README.md")
    assert is_supported_path("src/файл с пробелом.py")
    assert is_supported_path("a!b/c?d/[e]/{f}.py")
    assert is_supported_path("/".join(["s"] * 128))
    assert is_supported_path("a" * 1024)
    for path in (
        "",
        "/abs.py",
        "a//b.py",
        "a/",
        "./a.py",
        "a/../b.py",
        "a\\b.py",
        "a\x00b.py",
        "a\nb.py",
        "a\x7fb.py",
        "a" * 1025,
        "/".join(["s"] * 129),
        "bad\ud800.py",
    ):
        assert not is_supported_path(path), path


def test_incomplete_file_list_is_recorded_not_assumed_complete() -> None:
    diff_map = _build(_file(), files_changed=2)

    assert diff_map.file_list_complete is False
    assert diff_map.incomplete_reasons == ("file_count_mismatch",)


def test_digest_is_stable_for_the_same_input_and_changes_with_the_diff() -> None:
    first = _build(_file())

    assert first.digest == _build(_file()).digest
    assert len(first.digest) == 64
    assert first.digest != _build(_file(patch="@@ -1 +1 @@\n-old\n+other")).digest
    assert first.digest != _build(_file(), head_sha="e" * 40).digest
    assert first.files[0].file_id != _build(_file(), head_sha="e" * 40).files[0].file_id
