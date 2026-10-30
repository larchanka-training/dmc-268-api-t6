"""Snapshot sources of the #146 eval: a case tree plus ``diff.patch`` as the revisions, the
provider file list and the diff map the production context build reads.

A case directory holds ``base/`` (the repository at the case revision) and ``diff.patch``
(a ``git diff`` of the pull request). Everything the review input needs from a repository
host is derived from those two, deterministically and offline:

* the head tree is ``base/`` with the patch applied by ``git apply`` (the call the dataset
  validator makes);
* revision ids are synthetic 40-hex values derived from the case id; blob ids are the real
  git blob oids of the file bytes;
* each file of the patch becomes a ``VcsFile`` shaped as the GitHub files API lists it
  (hunk-only patch, added/modified/removed/renamed, no head blob for a removed file).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from review.scripts.validate_dataset import _git_env

type Tree = Mapping[str, bytes]

REPOSITORY_ID = UUID(int=146)
_LOCATOR = PullRequestLocator(146, "eval/rag", 1)

_DIFF_HEADER = re.compile(r"^diff --git ", re.MULTILINE)
_FIRST_HUNK = re.compile(r"^@@ ", re.MULTILINE)
_SIMILARITY = re.compile(r"^similarity index (?P<percent>\d+)%$", re.MULTILINE)


class PatchApplyError(ValueError):
    """``diff.patch`` does not apply to the base tree of its case."""


def git_blob_oid(raw: bytes) -> str:
    """The git blob object id of ``raw`` (what the provider names a file by)."""
    header = b"blob %d\0" % len(raw)
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def commit_id(case_id: str, side: str) -> str:
    """Synthetic 40-hex revision id of ``side`` (``base`` or ``head``), stable per case."""
    return hashlib.sha1(f"{case_id}:{side}".encode(), usedforsecurity=False).hexdigest()


def read_tree(root: Path) -> dict[str, bytes]:
    """The files under ``root`` by posix path, in path order."""
    # A bytecode cache left by a run over a fixture is not a file of the repository.
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def apply_patch(base: Tree, patch: str) -> dict[str, bytes]:
    """``base`` with ``patch`` applied by ``git apply``; ``base`` itself is not touched.

    Raises ``PatchApplyError`` with git's own message when the patch does not apply.
    """
    with tempfile.TemporaryDirectory(prefix="dmc146-apply-") as tmp:
        root = Path(tmp) / "tree"
        root.mkdir()
        for name, raw in base.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        patch_file = Path(tmp) / "diff.patch"
        patch_file.write_text(patch, encoding="utf-8", newline="")
        # The ceiling keeps git from finding a repository above the temp dir (TMPDIR inside a
        # work tree): there ``git apply`` applies only what the index tracks and exits 0.
        env = {**_git_env(), "GIT_CEILING_DIRECTORIES": tmp}
        done = subprocess.run(
            ["git", "apply", str(patch_file)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            detail = done.stderr.strip() or done.stdout.strip() or "git apply failed"
            raise PatchApplyError(f"diff.patch does not apply to the base tree: {detail}")
        head = read_tree(root)
        shutil.rmtree(root)
    return head


def _header_lines(section: str) -> list[str]:
    """The lines of a ``diff --git`` section before its first hunk."""
    lines: list[str] = []
    for line in section.split("\n"):
        if line.startswith("@@ "):
            break
        lines.append(line)
    return lines


def _status_of(header: list[str], *, has_hunks: bool) -> FileStatus | None:
    """The provider status of a section, or ``None`` for a change this module refuses
    (a mode-only change: no hunks and no rename)."""
    for line in header:
        if line.startswith("new file mode "):
            return "added"
        if line.startswith("deleted file mode "):
            return "removed"
        if line.startswith("rename from "):
            return "renamed"
    return "modified" if has_hunks else None


def _name(raw: str, prefix: str, first: str) -> str:
    """A path of a header line. Git quotes a path with non-ASCII or control characters
    C-style; the corpus keeps to plain paths, so a quoted one is refused."""
    if raw.startswith('"'):
        raise ValueError(f"quoted (C-style) path in a patch header is not supported: {first}")
    return raw.removeprefix(prefix)


def _header_names(header: list[str]) -> tuple[str, str]:
    """(old path, new path) of a ``diff --git`` section.

    ``rename from/to`` and ``---``/``+++`` lines name the paths exactly (git ends a name
    that has a space with a TAB there); a header without them (an empty added or removed
    file) is split in halves, which is exact because git names both sides alike there.
    """
    first = header[0]
    if first.startswith('diff --git "') or ' "b/' in first:
        raise ValueError(f"quoted (C-style) path in a patch header is not supported: {first}")
    old = new = None
    for line in header[1:]:
        if line.startswith("rename from "):
            old = _name(line.removeprefix("rename from "), "", first)
        elif line.startswith("rename to "):
            new = _name(line.removeprefix("rename to "), "", first)
        elif line.startswith("--- ") and old is None and line != "--- /dev/null":
            old = _name(line.removeprefix("--- ").rstrip("\t"), "a/", first)
        elif line.startswith("+++ ") and new is None and line != "+++ /dev/null":
            new = _name(line.removeprefix("+++ ").rstrip("\t"), "b/", first)
    if old is None or new is None:
        paths = first.removeprefix("diff --git ")
        half = (len(paths) - 1) // 2
        left, right = paths[:half], paths[half + 1 :]
        old = old if old is not None else left.removeprefix("a/")
        new = new if new is not None else right.removeprefix("b/")
    return old, new


def vcs_files(diff: str, base: Tree, head: Tree) -> tuple[VcsFile, ...]:
    """One ``VcsFile`` per file of ``diff``, as the provider lists a pull request's files.

    The patch of a file is hunk-only (no ``diff --git``/``index``/``---``/``+++``), and
    absent for a pure rename. Binary files, copies, empty added or removed files and
    mode-only changes are refused: the corpus does not model them.
    """
    starts = [match.start() for match in _DIFF_HEADER.finditer(diff)]
    files: list[VcsFile] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(diff)
        section = diff[start:end]
        header = _header_lines(section)
        old, new = _header_names(header)
        hunks = _FIRST_HUNK.search(section)
        status = _status_of(header, has_hunks=hunks is not None)
        if any(
            line == "GIT binary patch"
            or (line.startswith("Binary files ") and line.endswith(" differ"))
            for line in header
        ):
            raise ValueError(f"binary change is not supported: {new}")
        if any(line.startswith(("copy from ", "copy to ")) for line in header):
            raise ValueError(f"copy is not supported: {new}")
        if status is None:
            raise ValueError(f"change without hunks is not supported (mode-only?): {new}")
        patch = None
        additions = deletions = 0
        if hunks is not None:
            patch = section[hunks.start() :].rstrip("\n")
            lines = patch.split("\n")
            additions = sum(line.startswith("+") for line in lines)
            deletions = sum(line.startswith("-") for line in lines)
        elif status == "renamed":
            similarity = _SIMILARITY.search(section)
            if similarity is None or similarity.group("percent") != "100":
                raise ValueError(f"rename without hunks must be a 100% rename: {new}")
        else:
            raise ValueError(f"{status} file without hunks is not supported: {new}")
        if status == "removed":
            source = base
            blob_sha = None
        else:
            source = head
            blob_sha = git_blob_oid(head[new])
        files.append(
            VcsFile(
                filename=old if status == "removed" else new,
                status=status,
                blob_sha=blob_sha,
                previous_filename=old if status == "renamed" else None,
                additions=additions,
                deletions=deletions,
                changes=additions + deletions,
                patch=patch,
                size=len(source[old if status == "removed" else new]),
            )
        )
    return tuple(files)


@dataclass(frozen=True)
class CaseSnapshot:
    """The repository of one case at both revisions and the files the patch changes."""

    case_id: str
    base_id: str
    head_id: str
    base: Tree
    head: Tree
    diff: str
    files: tuple[VcsFile, ...]


def load_snapshot(case_dir: Path) -> CaseSnapshot:
    """Read ``base/`` and ``diff.patch`` of ``case_dir``; the head tree is derived."""
    case_id = case_dir.name
    base = read_tree(case_dir / "base")
    # Bytes, not ``read_text``: universal newlines would turn CRLF and a lone CR into LF.
    diff = (case_dir / "diff.patch").read_bytes().decode("utf-8")
    head = apply_patch(base, diff)
    return CaseSnapshot(
        case_id,
        commit_id(case_id, "base"),
        commit_id(case_id, "head"),
        base,
        head,
        diff,
        vcs_files(diff, base, head),
    )


def snapshot_diff_map(snapshot: CaseSnapshot, meta: PullRequestMeta) -> DiffMap:
    """The diff map of the case, with the base as ``diff_old_sha`` so removed and renamed
    files keep their old side (ADR 0001 §8.3). ``meta.files_changed`` must count the files."""
    pull_request = VcsPullRequest(
        locator=_LOCATOR, head_sha=snapshot.head_id, base_sha=snapshot.base_id, meta=meta
    )
    return build_diff_map(
        pull_request,
        snapshot.files,
        provider=VcsProviderName.GITHUB,
        repository_id=REPOSITORY_ID,
        diff_old_sha=snapshot.base_id,
    )


@dataclass
class SnapshotRevisionSource:
    """``RevisionSource`` over in-memory trees keyed by revision id."""

    trees: Mapping[str, Tree]
    complete: bool = True
    _blobs: dict[str, bytes] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for files in self.trees.values():
            for raw in files.values():
                self._blobs[git_blob_oid(raw)] = raw

    async def tree(self, revision: str) -> RevisionTree:
        files = self.trees[revision]
        return RevisionTree(
            revision, {path: git_blob_oid(raw) for path, raw in files.items()}, self.complete
        )

    async def read_blob(self, blob_sha: str) -> bytes:
        return self._blobs[blob_sha]
