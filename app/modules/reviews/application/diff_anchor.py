"""Inline anchor of a finding, validated against the diff map (spec §9.3).

The first rollout keeps the LLM contract ``path`` / ``start_line`` / ``line`` and anchors
on the new side only. A range is never moved, shortened or carried to the other side.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.modules.reviews.application.diff_map import (
    ChangeType,
    DiffFile,
    DiffHunk,
    DiffMap,
    DiffSide,
    PatchState,
)


class AnchorRejection(StrEnum):
    """Every reason of spec §9.3; ``resolve_anchor`` decides those marked as map-derived."""

    UNKNOWN_PATH = "unknown_path"  # map-derived
    WRONG_SIDE = "wrong_side"  # map-derived
    LINE_ABSENT = "line_absent"  # map-derived
    CROSS_HUNK_RANGE = "cross_hunk_range"  # map-derived
    RANGE_GAP = "range_gap"  # map-derived
    INCOMPLETE_DIFF = "incomplete_diff"  # map-derived
    ANCHOR_NOT_VISIBLE = "anchor_not_visible"
    IGNORED_FILE = "ignored_file"
    UNSUPPORTED_PROVIDER_ANCHOR = "unsupported_provider_anchor"


@dataclass(frozen=True)
class Anchor:
    """A valid inline position; ``start_line`` is absent for a single line."""

    file_id: str
    side: DiffSide
    start_line: int | None
    line: int
    hunk_id: str


def resolve_anchor(
    diff_map: DiffMap, *, path: str, start_line: int | None, line: int
) -> Anchor | AnchorRejection:
    """Return the new-side anchor of ``path`` lines, or the reason there is none."""
    if line < 1 or (start_line is not None and not 1 <= start_line < line):
        raise ValueError("anchor lines must satisfy 1 <= start_line < line")
    file = next((item for item in diff_map.files if item.new_path == path), None)
    if file is None:
        for item in diff_map.files:
            if item.change_type is ChangeType.DELETED and item.old_path == path:
                return AnchorRejection.WRONG_SIDE
            if item.omission_reason is not None and item.provider_new_path == path:
                # The file is in the diff, but its content is withheld.
                return AnchorRejection.INCOMPLETE_DIFF
        return AnchorRejection.UNKNOWN_PATH
    if not file.anchorable:
        return _unanchorable(file)
    hunk = _hunk_with_new_line(file, line)
    if hunk is None:
        return AnchorRejection.LINE_ABSENT
    if start_line is not None:
        start_hunk = _hunk_with_new_line(file, start_line)
        if start_hunk is None:
            return AnchorRejection.LINE_ABSENT
        if start_hunk is not hunk:
            return AnchorRejection.CROSS_HUNK_RANGE
        present = {row.new_line for row in hunk.rows}
        if any(number not in present for number in range(start_line, line + 1)):
            return AnchorRejection.RANGE_GAP
    return Anchor(file.file_id, DiffSide.RIGHT, start_line, line, hunk.hunk_id)


def _unanchorable(file: DiffFile) -> AnchorRejection:
    # A file without text lines (a pure rename, a mode change, a binary) has no such line.
    lineless = file.patch_state in {PatchState.METADATA_ONLY, PatchState.BINARY}
    if file.omission_reason is None and lineless:
        return AnchorRejection.LINE_ABSENT
    return AnchorRejection.INCOMPLETE_DIFF


def _hunk_with_new_line(file: DiffFile, line: int) -> DiffHunk | None:
    return next(
        (hunk for hunk in file.hunks if any(row.new_line == line for row in hunk.rows)), None
    )
