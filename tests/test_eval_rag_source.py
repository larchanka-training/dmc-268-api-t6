"""Snapshot sources of the #146 eval: a case tree and ``diff.patch`` become the revisions,
the provider file list and the diff map the production context build reads (Task 2)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.modules.reviews.application.diff_map import ChangeType, DiffMap, PatchState
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import VcsFile
from review.scripts.eval_rag_source import (
    CaseSnapshot,
    PatchApplyError,
    SnapshotRevisionSource,
    apply_patch,
    commit_id,
    git_blob_oid,
    load_snapshot,
    snapshot_diff_map,
    vcs_files,
)

_SPIKE = Path(__file__).parent / "fixtures" / "rag_eval" / "spike"

_STORE = "".join(f"def f{n}(x):\n    return x + {n}\n\n" for n in range(12))
_API = "".join(f"def g{n}(y):\n    return y * {n}\n\n" for n in range(12))
_BASE: dict[str, bytes] = {
    "pkg/store.py": _STORE.encode(),
    "pkg/api.py": _API.encode(),
    "pkg/old.py": b"def gone() -> int:\n    return 1\n",
    "pkg/keep.py": b"VALUE = 1\n",
}


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        check=True,
    )
    return done.stdout.decode()


def _write(root: Path, files: Mapping[str, bytes]) -> None:
    for name, raw in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def _patch_between(
    tmp_path: Path, after: Mapping[str, bytes], *options: str, base: Mapping[str, bytes] = _BASE
) -> str:
    """A real ``git diff -M`` from ``_BASE`` to ``after``, as a corpus author would take it."""
    repo = tmp_path / "authoring"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for path in base:
        (repo / path).unlink()
    _write(repo, after)
    _git(repo, "add", "-A")
    return _git(repo, "diff", "--cached", "-M", *options, "--no-color", "HEAD")


def _by_name(files: tuple[VcsFile, ...]) -> dict[str, VcsFile]:
    return {file.filename: file for file in files}


def _meta(count: int) -> PullRequestMeta:
    return PullRequestMeta(
        title="t",
        description=None,
        author="a",
        source_branch="eval",
        target_branch="main",
        labels=(),
        files_changed=count,
        lines_added=0,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
        head_sha=commit_id("T", "head"),
        commit_messages=(),
        commit_messages_incomplete=False,
    )


def _snapshot(
    tmp_path: Path, after: Mapping[str, bytes], base: Mapping[str, bytes] = _BASE
) -> CaseSnapshot:
    case = tmp_path / "RAG-T"
    _write(case / "base", base)
    (case / "diff.patch").write_text(_patch_between(tmp_path, after, base=base), encoding="utf-8")
    return load_snapshot(case)


_MODIFIED = {**_BASE, "pkg/keep.py": b"VALUE = 2\nOTHER = 3\n"}
_ADDED = {**_BASE, "pkg/new.py": b"def fresh() -> int:\n    return 2\n"}
_REMOVED = {path: raw for path, raw in _BASE.items() if path != "pkg/old.py"}
_RENAMED = {
    ("pkg/store2.py" if path == "pkg/store.py" else path): raw for path, raw in _BASE.items()
}
_RENAMED_EDITED = {
    **_RENAMED,
    "pkg/store2.py": _STORE.replace("x + 3", "x + 33").encode(),
}
_MULTI = {
    "pkg/store2.py": _RENAMED_EDITED["pkg/store2.py"],
    "pkg/api.py": _API.replace("y * 5", "y * 55").encode(),
    "pkg/new.py": b"def fresh() -> int:\n    return 2\n",
    "pkg/keep.py": _BASE["pkg/keep.py"],
}

# name -> (head tree, {filename: (status, previous, additions, deletions, has hunks)})
_CASES: dict[str, tuple[dict[str, bytes], dict[str, tuple[str, str | None, int, int, bool]]]] = {
    "modify": (_MODIFIED, {"pkg/keep.py": ("modified", None, 2, 1, True)}),
    "add": (_ADDED, {"pkg/new.py": ("added", None, 2, 0, True)}),
    "delete": (_REMOVED, {"pkg/old.py": ("removed", None, 0, 2, True)}),
    "rename": (_RENAMED, {"pkg/store2.py": ("renamed", "pkg/store.py", 0, 0, False)}),
    "rename-edit": (
        _RENAMED_EDITED,
        {"pkg/store2.py": ("renamed", "pkg/store.py", 1, 1, True)},
    ),
    "multi-file": (
        _MULTI,
        {
            "pkg/store2.py": ("renamed", "pkg/store.py", 1, 1, True),
            "pkg/api.py": ("modified", None, 1, 1, True),
            "pkg/new.py": ("added", None, 2, 0, True),
            "pkg/old.py": ("removed", None, 0, 2, True),
        },
    ),
}


@pytest.mark.parametrize("name", list(_CASES))
def test_a_patch_becomes_one_provider_file_per_changed_path(name: str, tmp_path: Path) -> None:
    head, expected = _CASES[name]
    snapshot = _snapshot(tmp_path, head)

    assert snapshot.head == head
    files = _by_name(snapshot.files)
    assert set(files) == set(expected)
    for filename, (status, previous, additions, deletions, has_hunks) in expected.items():
        file = files[filename]
        assert (file.status, file.previous_filename) == (status, previous)
        assert (file.additions, file.deletions) == (additions, deletions)
        assert file.changes == additions + deletions
        # The provider lists a hunk-only patch: no ``diff --git``/``index``/``---``/``+++``.
        assert (file.patch is not None) == has_hunks
        if file.patch is not None:
            assert file.patch.startswith("@@ ")
            assert not file.patch.endswith("\n")
        # A removed file has no head blob; every other file is named by its head blob oid.
        assert file.blob_sha == (None if status == "removed" else git_blob_oid(head[filename]))


@pytest.mark.parametrize("name", list(_CASES))
def test_the_diff_map_is_complete_and_keeps_the_base_for_removed_and_renamed(
    name: str, tmp_path: Path
) -> None:
    head, expected = _CASES[name]
    snapshot = _snapshot(tmp_path, head)

    diff_map = snapshot_diff_map(snapshot, _meta(len(expected)))

    assert diff_map.diff_old_sha == snapshot.base_id
    assert diff_map.head_sha == snapshot.head_id
    assert diff_map.file_list_complete
    assert {file.provider_new_path or file.provider_old_path for file in diff_map.files} == set(
        expected
    )
    for file in diff_map.files:
        # The statistics of ``VcsFile`` match the hunks: a miscount would be ``TRUNCATED``.
        assert file.patch_state in {PatchState.COMPLETE, PatchState.METADATA_ONLY}
    assert _change_types(diff_map) == {
        filename: ChangeType(
            {"modified": "modified", "added": "added", "removed": "deleted", "renamed": "renamed"}[
                status
            ]
        )
        for filename, (status, *_rest) in expected.items()
    }


def _change_types(diff_map: DiffMap) -> dict[str, ChangeType]:
    return {
        (file.provider_new_path or file.provider_old_path or ""): file.change_type
        for file in diff_map.files
    }


def test_the_blob_oid_is_what_git_hash_object_prints(tmp_path: Path) -> None:
    repo = tmp_path / "hash"
    repo.mkdir()
    _git(repo, "init", "-q")
    for raw in (b"", b"x", b"a\nb\n", "tab\tund ü\n".encode(), bytes(range(256))):
        assert git_blob_oid(raw) == _git(repo, "hash-object", "--stdin", stdin=raw).strip()


def test_the_head_tree_is_the_base_with_the_patch_applied_and_base_is_untouched(
    tmp_path: Path,
) -> None:
    patch = _patch_between(tmp_path, _MULTI)
    before = dict(_BASE)

    assert apply_patch(_BASE, patch) == _MULTI
    assert before == _BASE


def test_a_patch_that_does_not_apply_is_a_clear_error(tmp_path: Path) -> None:
    patch = _patch_between(tmp_path, _MODIFIED)
    drifted = {**_BASE, "pkg/keep.py": b"VALUE = 9\n"}

    with pytest.raises(PatchApplyError, match=r"pkg/keep\.py"):
        apply_patch(drifted, patch)


def test_a_patch_for_a_file_the_base_lacks_is_a_clear_error(tmp_path: Path) -> None:
    patch = _patch_between(tmp_path, _REMOVED)
    without = {path: raw for path, raw in _BASE.items() if path != "pkg/old.py"}

    with pytest.raises(PatchApplyError, match=r"pkg/old\.py"):
        apply_patch(without, patch)


def test_a_change_without_hunks_other_than_a_pure_rename_is_refused(tmp_path: Path) -> None:
    """A mode-only change has no lines: the eval's corpus does not model it."""
    mode_only = "diff --git a/pkg/keep.py b/pkg/keep.py\nold mode 100644\nnew mode 100755\n"

    with pytest.raises(ValueError, match=r"pkg/keep\.py"):
        vcs_files(mode_only, _BASE, _BASE)


def test_commit_ids_are_stable_per_case_and_differ_between_cases_and_sides() -> None:
    assert commit_id("RAG-01", "base") == "d22f4893f05393ca688aebba25d6cfeeb96eb54e"
    assert commit_id("RAG-01", "base") == commit_id("RAG-01", "base")
    ids = {commit_id(case, side) for case in ("RAG-01", "RAG-02") for side in ("base", "head")}
    assert len(ids) == 4
    assert all(len(value) == 40 and int(value, 16) >= 0 for value in ids)


def test_the_snapshot_source_serves_trees_by_revision_and_blobs_by_oid() -> None:
    head = {**_BASE, "pkg/keep.py": b"VALUE = 2\n"}
    source = SnapshotRevisionSource({"b": _BASE, "h": head})

    tree = asyncio.run(source.tree("h"))

    assert tree.complete
    assert tree.blob_shas["pkg/keep.py"] == git_blob_oid(b"VALUE = 2\n")
    assert asyncio.run(source.read_blob(tree.blob_shas["pkg/keep.py"])) == b"VALUE = 2\n"
    assert asyncio.run(source.read_blob(git_blob_oid(_BASE["pkg/keep.py"]))) == b"VALUE = 1\n"


def test_load_snapshot_derives_the_spike_head_from_its_patch() -> None:
    snapshot = load_snapshot(_SPIKE)

    assert snapshot.base["pkg/store.py"].decode().count("dry_run") == 0
    assert b"def save(item: Item, *, dry_run: bool) -> Item:" in snapshot.head["pkg/store.py"]
    # Only the changed file differs; the rest of the head is the base, byte for byte.
    assert {path for path in snapshot.head if snapshot.head[path] != snapshot.base[path]} == {
        "pkg/store.py"
    }
    assert snapshot.base_id == commit_id("spike", "base")
    assert snapshot.head_id == commit_id("spike", "head")
    assert [(file.filename, file.status) for file in snapshot.files] == [
        ("pkg/store.py", "modified")
    ]


def test_apply_patch_does_not_see_a_repository_around_its_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a work tree ``git apply`` skips files outside the index and exits 0."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    monkeypatch.setattr(tempfile, "tempdir", str(outer))
    patch = _patch_between(tmp_path, _ADDED)

    assert apply_patch(_BASE, patch) == _ADDED


def test_apply_patch_ignores_an_exported_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git rebase --exec`` exports ``GIT_DIR``: it must not redirect the apply."""
    patch = _patch_between(tmp_path, _ADDED)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))

    assert apply_patch(_BASE, patch) == _ADDED


_SPACED = {**_BASE, "pkg/my file.py": b"def f() -> int:\n    return 1\n"}
_SPACED_BASE = {**_BASE, "pkg/a b.py": b"def g() -> int:\n    return 2\n"}
_SPACED_RENAME = {
    ("pkg/my store.py" if path == "pkg/store.py" else path): raw for path, raw in _BASE.items()
}


@pytest.mark.parametrize(
    ("base", "after", "filename", "status", "previous"),
    [
        (_BASE, _SPACED, "pkg/my file.py", "added", None),
        (_BASE, _SPACED_RENAME, "pkg/my store.py", "renamed", "pkg/store.py"),
        (_SPACED_BASE, _BASE, "pkg/a b.py", "removed", None),
    ],
    ids=["add", "rename", "delete"],
)
def test_a_path_with_a_space_is_a_file_of_the_patch(
    tmp_path: Path,
    base: dict[str, bytes],
    after: dict[str, bytes],
    filename: str,
    status: str,
    previous: str | None,
) -> None:
    snapshot = _snapshot(tmp_path, after, base)

    [file] = snapshot.files
    assert (file.filename, file.status, file.previous_filename) == (filename, status, previous)
    assert snapshot.head == after


def test_a_copy_with_edits_is_refused_not_read_as_a_modification(tmp_path: Path) -> None:
    copied = {**_BASE, "pkg/api2.py": _API.replace("y * 5", "y * 55").encode()}
    patch = _patch_between(tmp_path, copied, "-C", "-C")
    assert "copy from pkg/api.py" in patch  # the premise: git did detect a copy

    with pytest.raises(ValueError, match=r"copy.*pkg/api2\.py"):
        vcs_files(patch, _BASE, copied)


_QUOTED = "pkg/\u00e9.py"
_REFUSED: dict[str, tuple[dict[str, bytes], str, str]] = {
    "binary": ({**_BASE, "pkg/blob.bin": b"\x00\x01\x02"}, "--binary", r"binary.*blob\.bin"),
    "quoted-add": ({**_BASE, _QUOTED: b"X = 1\n"}, "-M", "quoted"),
    "quoted-rename": (
        {("pkg/\u00e9.py" if path == "pkg/store.py" else path): raw for path, raw in _BASE.items()},
        "-M",
        "quoted",
    ),
    "empty-added": ({**_BASE, "pkg/empty.py": b""}, "-M", r"added.*empty\.py"),
}


@pytest.mark.parametrize("name", ["binary", "quoted-add", "quoted-rename", "empty-added"])
def test_changes_the_corpus_does_not_model_are_refused_with_the_path(
    name: str, tmp_path: Path
) -> None:
    after, option, message = _REFUSED[name]
    patch = _patch_between(tmp_path, after, option)

    with pytest.raises(ValueError, match=message):
        vcs_files(patch, _BASE, after)


def test_a_rename_without_hunks_below_one_hundred_percent_is_refused() -> None:
    patch = (
        "diff --git a/pkg/keep.py b/pkg/kept.py\n"
        "similarity index 90%\nrename from pkg/keep.py\nrename to pkg/kept.py\n"
    )
    head = {"pkg/kept.py": _BASE["pkg/keep.py"]}

    with pytest.raises(ValueError, match=r"100%.*pkg/kept\.py"):
        vcs_files(patch, _BASE, head)


def test_added_lines_that_read_like_git_headers_are_content(tmp_path: Path) -> None:
    tricky = {**_BASE, "pkg/doc.py": b"GIT binary patch\nBinary files a and b differ\n"}
    snapshot = _snapshot(tmp_path, tricky)

    assert [(file.filename, file.status, file.additions) for file in snapshot.files] == [
        ("pkg/doc.py", "added", 2)
    ]


def test_load_snapshot_keeps_the_patch_byte_exact(tmp_path: Path) -> None:
    # A lone CR inside an added line: reading the patch with universal newlines turns it into LF.
    after = {**_BASE, "pkg/keep.py": b"VALUE = 2\rOTHER = 3\n"}

    snapshot = _snapshot(tmp_path, after)

    assert "VALUE = 2\rOTHER = 3\n" in snapshot.diff
    assert snapshot.head["pkg/keep.py"] == after["pkg/keep.py"]
