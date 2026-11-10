"""Which changed-file names a run can store unchanged (RFS §11, CVS §9.1, #497 part 2).

An omitted file is isolated by its stored provider names. A name the run cannot store
unchanged — a NUL, a lone surrogate, more than the column width — leaves its source with
no identity to withhold, so the snapshot path fails the run instead. The run itself is in
``tests/test_snapshot_path_e2e_postgres.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String

from app.modules.reviews.application.get_run_diff import DiffSnapshot, has_storable_provider_names
from app.modules.reviews.infrastructure.models import CodeChangeDiff


@pytest.mark.parametrize(
    ("name", "storable"),
    [
        ("d/" * 511 + "xy", True),
        ("é" * 1024, True),
        ("d/" * 511 + "xyz", False),
        ("a\x00b.py", False),
        ("a\ud800b.py", False),
        ("a\udfffb.py", False),
    ],
    ids=[
        "1024-chars",
        "1024-two-byte-chars",
        "1025-chars",
        "nul",
        "high-surrogate",
        "low-surrogate",
    ],
)
def test_a_name_is_storable_only_when_the_columns_hold_it_unchanged(
    name: str, storable: bool
) -> None:
    added = DiffSnapshot(filename=name, patch=None, status="added")
    renamed = DiffSnapshot(
        filename="src/ok.py", patch=None, status="renamed", previous_filename=name
    )

    assert has_storable_provider_names(added) is storable
    assert has_storable_provider_names(renamed) is storable


def test_both_stored_name_columns_hold_1024_characters() -> None:
    # The width the 1024/1025 rows above hold a name to (RFS §4.2).
    columns = CodeChangeDiff.__table__.c

    for column in (columns.filename, columns.previous_filename):
        assert isinstance(column.type, String)
        assert column.type.length == 1024
