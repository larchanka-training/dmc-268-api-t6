"""Estimated bytes of graph index rows, for the storage quotas of ADR 0001 §7 (#142).

An estimate, not the size on disk: a fixed overhead per row plus the UTF-8 bytes of each
text column, weighted by `w(c)` = 1 + the number of indexes, the primary key included,
that contain column `c`. The adapter computes the same formula in SQL; a parity test
keeps the two halves equal.
"""

from __future__ import annotations

from collections.abc import Mapping

ROW_OVERHEAD_BYTES = 160

COLUMN_WEIGHTS: Mapping[str, Mapping[str, int]] = {
    "code_index_files": {"path": 2, "blob_sha": 1},
    "code_index_symbols": {"path": 2, "qualified_name": 2},
    "code_index_edges": {"path": 2, "target_path": 3, "target_name": 3},
    "code_index_imports": {"path": 2, "specifier": 2, "target_path": 2},
}


def row_bytes(table: str, values: Mapping[str, str | None]) -> int:
    """The estimate of one row of `table`; `values` holds every weighted column, and a
    `None` value counts as empty."""
    return ROW_OVERHEAD_BYTES + sum(
        weight * len((values[column] or "").encode())
        for column, weight in COLUMN_WEIGHTS[table].items()
    )
