"""Provenance of one context fragment (CONTEXT_AND_VERIFICATION_SPEC §3.1).

Every fragment that may reach a model call says where it came from: role, revision, path
and source line ranges. The numbers are lines of the source file, never lines of a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.modules.reviews.application.diff_map import (
    DiffFile,
    DiffHunk,
    DiffMap,
    DiffSide,
    RowKind,
)


class SourceRole(StrEnum):
    POLICY_DOCUMENT = "policy_document"
    SERVICE_DEFAULTS = "service_defaults"
    CONVENTIONS = "conventions"
    PR_METADATA = "pr_metadata"
    DIFF = "diff"
    SOURCE_EXCERPT = "source_excerpt"
    AST_EXCERPT = "ast_excerpt"
    CANDIDATE = "candidate"
    PREVIOUS_OUTPUT = "previous_output"


@dataclass(frozen=True)
class LineRange:
    """An inclusive range of 1-based source lines."""

    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if not 1 <= self.start_line <= self.end_line:
            raise ValueError("line range must satisfy 1 <= start_line <= end_line")


@dataclass(frozen=True)
class SourceRef:
    """Where a fragment came from; ``origin_paths`` keeps earlier names of a renamed file."""

    role: SourceRole
    repository_id: UUID
    revision: str | None
    path: str | None
    blob_sha: str | None
    origin_paths: tuple[str, ...]
    side: DiffSide | None
    ranges: tuple[LineRange, ...]
    reason: str


def hunk_source_refs(diff_map: DiffMap, file: DiffFile, hunk: DiffHunk) -> tuple[SourceRef, ...]:
    """Provenance of one hunk of an anchorable file, one reference per side it has.

    The new side is the head revision. The old side is given when the hunk removes lines
    and the old revision of the diff is known: removed text is never attributed to the
    head, and an unknown old revision is not presented as a confirmed source.
    """
    if not file.anchorable:
        raise ValueError("only a file with a complete diff has hunk provenance")
    refs: list[SourceRef] = []
    old_path, new_path = file.old_path, file.new_path
    if new_path is not None and hunk.new_count:
        refs.append(
            SourceRef(
                role=SourceRole.DIFF,
                repository_id=diff_map.repository_id,
                revision=diff_map.head_sha,
                path=new_path,
                blob_sha=file.blob_sha,
                origin_paths=(old_path,) if old_path is not None and old_path != new_path else (),
                side=DiffSide.RIGHT,
                ranges=(LineRange(hunk.new_start, hunk.new_start + hunk.new_count - 1),),
                reason="changed_hunk",
            )
        )
    removes = any(row.kind is RowKind.REMOVED for row in hunk.rows)
    if old_path is not None and removes and diff_map.diff_old_sha is not None:
        refs.append(
            SourceRef(
                role=SourceRole.DIFF,
                repository_id=diff_map.repository_id,
                revision=diff_map.diff_old_sha,
                path=old_path,
                blob_sha=None,
                origin_paths=(),
                side=DiffSide.LEFT,
                ranges=(LineRange(hunk.old_start, hunk.old_start + hunk.old_count - 1),),
                reason="changed_hunk",
            )
        )
    return tuple(refs)
