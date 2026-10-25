"""Findings diff map rebuilt from stored snapshots: the stored name and line numbers (#168).

After #229 the findings line check resolves anchors against a DiffMap rebuilt from the
stored ``code_change_diffs`` rows. The path comes from ``CodeChangeDiff.filename`` and the
hunks are split on LF only, so neither the ``diff --git`` header, a line separator inside an
added line, nor a bare ``rename to`` line in a hunk can rename the file or shift its line
numbers.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.modules.reviews.application.diff_anchor import Anchor, resolve_anchor
from app.modules.reviews.application.diff_map import (
    ChangeType,
    DiffMap,
    DiffSide,
    PatchState,
    RowKind,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots

_PATH = "src/q b/r.py"


class _EchoRepository:
    """Hands back the snapshots as ``StoreDiffSnapshot`` normalized them."""

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        return snapshots


def _findings_diff_map(patch: str, *, additions: int) -> DiffMap:
    """The diff map the findings check rebuilds from the row ``StoreDiffSnapshot`` stored."""
    stored = asyncio.run(
        StoreDiffSnapshot(_EchoRepository()).execute(
            run_id=UUID(int=1),
            code_change_id=UUID(int=2),
            head_sha="a" * 40,
            files=[
                DiffSnapshot(
                    _PATH,
                    patch,
                    "d" * 40,
                    "modified",
                    additions=additions,
                    deletions=0,
                    changes=additions,
                )
            ],
        )
    )
    # The stored UI patch carries the header whose first " b/" cut gives a wrong name.
    assert stored[0].patch is not None
    assert stored[0].patch.startswith(f"diff --git a/{_PATH} b/{_PATH}\n")
    return diff_map_from_snapshots(
        repository_id=UUID(int=3),
        code_change_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        diff_old_sha=None,
        snapshots=stored,
    )


# Each separator ends a line for ``str.splitlines``, not for a unified diff.
@pytest.mark.parametrize(
    "added",
    [
        pytest.param("y\u2028rename to evil.py", id="line-separator"),
        pytest.param("y\x0crename to evil.py", id="form-feed"),
        pytest.param("y\x85rename to evil.py", id="next-line"),
    ],
)
def test_findings_diff_map_keeps_the_stored_name_and_unshifted_lines(added: str) -> None:
    diff_map = _findings_diff_map(f"@@ -1,2 +1,4 @@\n a\n+{added}\n+z\n b", additions=2)

    [file] = diff_map.files
    assert (file.old_path, file.new_path, file.change_type) == (_PATH, _PATH, ChangeType.MODIFIED)
    assert [
        (row.kind, row.old_line, row.new_line, row.text) for hunk in file.hunks for row in hunk.rows
    ] == [
        (RowKind.CONTEXT, 1, 1, "a"),
        (RowKind.ADDED, None, 2, added),
        (RowKind.ADDED, None, 3, "z"),
        (RowKind.CONTEXT, 2, 4, "b"),
    ]
    assert resolve_anchor(diff_map, path=_PATH, start_line=None, line=2) == Anchor(
        file.file_id, DiffSide.RIGHT, None, 2, file.hunks[0].hunk_id
    )


def test_findings_diff_map_keeps_the_stored_name_for_a_bare_rename_line_in_a_hunk() -> None:
    # Without the bare line the hunk is complete; with it the patch is malformed, not a rename.
    diff_map = _findings_diff_map("@@ -1,2 +1,3 @@\n a\n+y\nrename to evil.py\n b", additions=1)

    [file] = diff_map.files
    assert (file.old_path, file.new_path, file.patch_state) == (_PATH, _PATH, PatchState.MALFORMED)
