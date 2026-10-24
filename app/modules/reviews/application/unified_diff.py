"""Low-level walk over the hunks of a unified diff patch.

Shared by the L1 prompt input (``vcs_diff.parse_file_patch``) and the diff map
(``diff_map.build_diff_map``). They differ in what they build from a hunk and in how
they treat a carriage return, so each keeps its own output contract and its own line
framing; the reading of headers, prefixes and line counts lives here once.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NO_NEWLINE_MARKER = "\\ No newline at end of file"

type RowKind = Literal["context", "added", "removed"]


class UnifiedDiffError(ValueError):
    """The patch does not follow its own hunk headers."""


@dataclass(frozen=True)
class HunkRow:
    """One line of a hunk; the side it does not exist on has no number."""

    kind: RowKind
    old_line: int | None
    new_line: int | None
    text: str
    old_cursor: int
    new_cursor: int


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    rows: tuple[HunkRow, ...]


def split_lf(patch: str) -> list[str]:
    """Split only on LF; other Unicode line separators can be literal source content."""
    if not patch:
        return []
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def parse_hunks(lines: Iterable[str]) -> tuple[Hunk, ...]:
    """Read hunks from framed patch lines; a count that disagrees with its header raises.

    An absent count in ``@@ -A[,B] +C[,D] @@`` is one line. The "no newline at end of
    file" marker is metadata, not a row.
    """
    hunks: list[Hunk] = []
    rows: list[HunkRow] = []
    header: tuple[int, int, int, int] | None = None
    old_line = new_line = old_left = new_left = 0
    for line in lines:
        match = HUNK_HEADER.match(line)
        if match is not None:
            if header is not None:
                if old_left or new_left:
                    raise UnifiedDiffError("GitHub diff hunk ended before its declared line counts")
                hunks.append(Hunk(*header, tuple(rows)))
            old_line, new_line = int(match.group(1)), int(match.group(3))
            old_left = 1 if match.group(2) is None else int(match.group(2))
            new_left = 1 if match.group(4) is None else int(match.group(4))
            header = (old_line, old_left, new_line, new_left)
            rows = []
            continue
        if header is None:
            raise UnifiedDiffError("GitHub diff has no hunk header")
        if line == NO_NEWLINE_MARKER:
            continue
        if line.startswith("+"):
            rows.append(HunkRow("added", None, new_line, line[1:], old_line, new_line))
            new_line, new_left = new_line + 1, new_left - 1
        elif line.startswith("-"):
            rows.append(HunkRow("removed", old_line, None, line[1:], old_line, new_line))
            old_line, old_left = old_line + 1, old_left - 1
        elif line.startswith(" "):
            rows.append(HunkRow("context", old_line, new_line, line[1:], old_line, new_line))
            old_line, new_line = old_line + 1, new_line + 1
            old_left, new_left = old_left - 1, new_left - 1
        else:
            raise UnifiedDiffError("GitHub diff hunk has an unknown line")
        if old_left < 0 or new_left < 0:
            raise UnifiedDiffError("GitHub diff hunk exceeded its declared line counts")
    if header is None or old_left or new_left:
        raise UnifiedDiffError("GitHub diff hunk is incomplete")
    hunks.append(Hunk(*header, tuple(rows)))
    return tuple(hunks)
