"""Which changed-file names a run can hold without loss (RFS §11, CVS §9.1, #497 part 2).

An omitted file is isolated by its stored provider names. A name the run cannot store
unchanged — a NUL, a lone surrogate, more than the column width — leaves its source with
no identity to withhold, so the snapshot path fails the run instead. The run itself is in
``tests/test_unprovable_isolation_postgres.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String

from app.modules.reviews.application.get_run_diff import (
    MAX_STORED_NAME_LENGTH,
    DiffSnapshot,
    holds_without_loss,
)
from app.modules.reviews.infrastructure.models import CodeChangeDiff


@pytest.mark.parametrize(
    ("name", "held"),
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
def test_a_name_is_held_only_when_it_can_be_stored_unchanged(name: str, held: bool) -> None:
    added = DiffSnapshot(filename=name, patch=None, status="added")
    renamed = DiffSnapshot(
        filename="src/ok.py", patch=None, status="renamed", previous_filename=name
    )

    assert holds_without_loss(added) is held
    assert holds_without_loss(renamed) is held


def test_the_held_length_is_the_width_of_the_stored_name_columns() -> None:
    columns = CodeChangeDiff.__table__.c

    for column in (columns.filename, columns.previous_filename):
        assert isinstance(column.type, String)
        assert column.type.length == MAX_STORED_NAME_LENGTH
