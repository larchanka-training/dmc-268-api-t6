"""DiffMap/v1: an immutable map of one pull request diff.

The map holds provider facts only (CONTEXT_AND_VERIFICATION_SPEC §9.1-9.2): which files
changed, their hunks and the line numbers of both sides. It carries no decision about
ignore rules or about what a model was shown, so the same VCS input always yields the
same map and the map can be rebuilt from the stored patches.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from app.modules.reviews.application.unified_diff import UnifiedDiffError, parse_hunks, split_lf
from app.modules.reviews.application.vcs_diff import VcsFile, VcsPullRequest

DIFF_MAP_SCHEMA_VERSION = 1
DIFF_MAP_PARSER_VERSION = "unified-hunks/v1"
MAX_PATH_BYTES = 1024
MAX_PATH_SEGMENTS = 128

_FILE_COUNT_MISMATCH = "file_count_mismatch"


class VcsProviderName(StrEnum):
    GITHUB = "github"


class DiffSide(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class ChangeType(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    MODE_CHANGED = "mode_changed"
    UNSUPPORTED = "unsupported"


class PatchState(StrEnum):
    COMPLETE = "complete"
    MISSING = "missing"
    TRUNCATED = "truncated"
    MALFORMED = "malformed"
    BINARY = "binary"
    UNSUPPORTED = "unsupported"
    METADATA_ONLY = "metadata_only"


class DiffOmissionReason(StrEnum):
    """Why the content of a file must not be used, independent of the patch state."""

    UNSUPPORTED_PATH = "unsupported_path"
    IDENTITY_UNRESOLVED = "identity_unresolved"


class RowKind(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class DiffRow:
    """One line of a hunk; a side the line does not exist on has no number."""

    row_id: str
    kind: RowKind
    old_line: int | None
    new_line: int | None
    text: str
    old_cursor: int
    new_cursor: int


@dataclass(frozen=True)
class DiffHunk:
    hunk_id: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    # Both sides consumed exactly their declared line counts.
    complete: bool
    rows: tuple[DiffRow, ...]


@dataclass(frozen=True)
class DiffFile:
    """One changed file; ``old_path`` is absent for an addition, ``new_path`` for a deletion.

    ``old_path`` and ``new_path`` are canonical: a name that violates the path contract
    is never stored there, so nothing built from them can carry it to a model. The names
    as the provider sent them stay in ``provider_old_path`` and ``provider_new_path``
    for diagnostics only.
    """

    file_id: str
    old_path: str | None
    new_path: str | None
    provider_old_path: str | None
    provider_new_path: str | None
    change_type: ChangeType
    blob_sha: str | None
    additions: int
    deletions: int
    patch_state: PatchState
    patch_digest: str | None
    omission_reason: DiffOmissionReason | None
    hunks: tuple[DiffHunk, ...]

    @property
    def anchorable(self) -> bool:
        """Whether the hunks are the complete, usable diff of this file."""
        return self.omission_reason is None and self.patch_state is PatchState.COMPLETE


@dataclass(frozen=True)
class DiffMap:
    """The diff of one pull request head; ``diff_old_sha`` is its merge base, when known."""

    schema_version: int
    parser_version: str
    provider: VcsProviderName
    repository_id: UUID
    code_change_number: int
    policy_base_sha: str
    head_sha: str
    diff_old_sha: str | None
    file_list_complete: bool
    incomplete_reasons: tuple[str, ...]
    files: tuple[DiffFile, ...]
    digest: str

    def file_by_id(self, file_id: str) -> DiffFile | None:
        return next((file for file in self.files if file.file_id == file_id), None)


def is_supported_path(path: str) -> bool:
    """Whether a provider path is a canonical repository-relative path (spec §9.1)."""
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if not path or len(encoded) > MAX_PATH_BYTES or "\\" in path:
        return False
    # Every Unicode control character: C0, DEL and C1 (U+0080-U+009F) alike.
    if any(unicodedata.category(char) == "Cc" for char in path):
        return False
    segments = path.split("/")
    return len(segments) <= MAX_PATH_SEGMENTS and all(
        segment not in {"", ".", ".."} for segment in segments
    )


def build_diff_map(
    pull_request: VcsPullRequest,
    files: tuple[VcsFile, ...],
    *,
    provider: VcsProviderName,
    repository_id: UUID,
    diff_old_sha: str | None,
) -> DiffMap:
    """Build the map from one complete provider read; no network and no database."""
    identity: list[str | int] = [
        provider.value,
        str(repository_id),
        pull_request.locator.number,
        pull_request.head_sha,
    ]
    diff_files = tuple(_build_file(identity, file) for file in files)
    complete = len(files) == pull_request.meta.files_changed
    incomplete_reasons = () if complete else (_FILE_COUNT_MISMATCH,)
    payload = {
        "schema_version": DIFF_MAP_SCHEMA_VERSION,
        "parser_version": DIFF_MAP_PARSER_VERSION,
        "provider": provider.value,
        "repository_id": str(repository_id),
        "code_change_number": pull_request.locator.number,
        "policy_base_sha": pull_request.base_sha,
        "head_sha": pull_request.head_sha,
        "diff_old_sha": diff_old_sha,
        "file_list_complete": complete,
        "incomplete_reasons": list(incomplete_reasons),
        "files": [_file_digest_entry(file) for file in diff_files],
    }
    return DiffMap(
        schema_version=DIFF_MAP_SCHEMA_VERSION,
        parser_version=DIFF_MAP_PARSER_VERSION,
        provider=provider,
        repository_id=repository_id,
        code_change_number=pull_request.locator.number,
        policy_base_sha=pull_request.base_sha,
        head_sha=pull_request.head_sha,
        diff_old_sha=diff_old_sha,
        file_list_complete=complete,
        incomplete_reasons=incomplete_reasons,
        files=diff_files,
        digest=_digest(payload),
    )


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _file_digest_entry(file: DiffFile) -> dict[str, object]:
    return {
        "file_id": file.file_id,
        "old_path": file.old_path,
        "new_path": file.new_path,
        "provider_old_path": file.provider_old_path,
        "provider_new_path": file.provider_new_path,
        "change_type": file.change_type.value,
        "blob_sha": file.blob_sha,
        "additions": file.additions,
        "deletions": file.deletions,
        "patch_state": file.patch_state.value,
        "patch_digest": file.patch_digest,
        "omission_reason": None if file.omission_reason is None else file.omission_reason.value,
    }


def _change_type(file: VcsFile) -> ChangeType:
    match file.status:
        case "added":
            return ChangeType.ADDED
        case "modified":
            return ChangeType.MODIFIED
        case "removed":
            return ChangeType.DELETED
        case "renamed":
            return ChangeType.RENAMED
        case "copied":
            return ChangeType.COPIED
        case "changed":
            # GitHub reports a mode or type change as "changed"; with lines it is a modification.
            return ChangeType.MODIFIED if file.changes else ChangeType.MODE_CHANGED
        case _:
            return ChangeType.UNSUPPORTED


def _paths(file: VcsFile, change_type: ChangeType) -> tuple[str | None, str | None]:
    if change_type is ChangeType.ADDED:
        return None, file.filename
    if change_type is ChangeType.DELETED:
        return file.filename, None
    if change_type in {ChangeType.RENAMED, ChangeType.COPIED}:
        return file.previous_filename, file.filename
    return file.filename, file.filename


def _build_file(identity: list[str | int], file: VcsFile) -> DiffFile:
    change_type = _change_type(file)
    old_path, new_path = _paths(file, change_type)
    # The identity uses the provider names: two rejected names are still two files.
    file_id = _digest([*identity, change_type.value, old_path, new_path])[:32]
    omission = _omission(change_type, old_path, new_path)
    patch_digest = None if file.patch is None else sha256(file.patch.encode("utf-8")).hexdigest()
    state, hunks = _patch_state(file_id, file)
    return DiffFile(
        file_id=file_id,
        old_path=_canonical(old_path),
        new_path=_canonical(new_path),
        provider_old_path=old_path,
        provider_new_path=new_path,
        change_type=change_type,
        blob_sha=file.blob_sha,
        additions=file.additions,
        deletions=file.deletions,
        patch_state=state,
        patch_digest=patch_digest,
        # The content of a file with an unusable identity is never exposed through the map.
        omission_reason=omission,
        hunks=() if omission is not None else hunks,
    )


def _canonical(path: str | None) -> str | None:
    return path if path is not None and is_supported_path(path) else None


def _omission(
    change_type: ChangeType, old_path: str | None, new_path: str | None
) -> DiffOmissionReason | None:
    if change_type in {ChangeType.RENAMED, ChangeType.COPIED} and old_path is None:
        return DiffOmissionReason.IDENTITY_UNRESOLVED
    if any(path is not None and not is_supported_path(path) for path in (old_path, new_path)):
        return DiffOmissionReason.UNSUPPORTED_PATH
    return None


def _patch_state(file_id: str, file: VcsFile) -> tuple[PatchState, tuple[DiffHunk, ...]]:
    if file.patch is None or file.patch == "":
        state = PatchState.METADATA_ONLY if file.changes == 0 else PatchState.MISSING
        return state, ()
    if "\x00" in file.patch:
        return PatchState.BINARY, ()
    lines = _split_patch(file.patch)
    if any(line.startswith("@@@") for line in lines):
        return PatchState.UNSUPPORTED, ()
    try:
        hunks = _parse_hunks(file_id, lines)
    except _MalformedPatch:
        return PatchState.MALFORMED, ()
    added = sum(row.kind is RowKind.ADDED for hunk in hunks for row in hunk.rows)
    removed = sum(row.kind is RowKind.REMOVED for hunk in hunks for row in hunk.rows)
    if added != file.additions or removed != file.deletions:
        # The provider statistics describe more lines than the patch it returned.
        return PatchState.TRUNCATED, hunks
    return PatchState.COMPLETE, hunks


class _MalformedPatch(ValueError):
    pass


def _split_patch(patch: str) -> list[str]:
    """Patch lines for the diff map: a CR is kept unless it frames the whole patch.

    A CR is removed only as CRLF framing of the patch itself, recognised when every
    line, hunk headers included, ends with it. Otherwise a trailing CR belongs to the
    source line (a file with CRLF endings) and is kept.
    """
    lines = split_lf(patch)
    if lines and all(line.endswith("\r") for line in lines):
        return [line[:-1] for line in lines]
    return lines


def _parse_hunks(file_id: str, lines: list[str]) -> tuple[DiffHunk, ...]:
    try:
        parsed = parse_hunks(lines)
    except UnifiedDiffError as exc:
        raise _MalformedPatch(str(exc)) from exc
    hunks: list[DiffHunk] = []
    old_end = new_end = 0
    for hunk in parsed:
        # Checks of the map beyond the line counts: every existing line has a number,
        # and hunks follow each other on both sides.
        if (hunk.old_count and not hunk.old_start) or (hunk.new_count and not hunk.new_start):
            raise _MalformedPatch("an existing line cannot have number zero")
        if hunk.old_start < old_end or hunk.new_start < new_end:
            raise _MalformedPatch("hunks overlap or are out of order")
        old_end, new_end = hunk.old_start + hunk.old_count, hunk.new_start + hunk.new_count
        hunk_id = f"{file_id}:{len(hunks)}"
        rows = tuple(
            DiffRow(
                f"{hunk_id}:{index}",
                RowKind(row.kind),
                row.old_line,
                row.new_line,
                row.text,
                row.old_cursor,
                row.new_cursor,
            )
            for index, row in enumerate(hunk.rows)
        )
        hunks.append(
            DiffHunk(
                hunk_id, hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count, True, rows
            )
        )
    return tuple(hunks)
