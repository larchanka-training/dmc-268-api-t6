from sqlalchemy import Table, UniqueConstraint

from app.modules.reviews.infrastructure.models import CodeChangeDiff


def test_diff_snapshots_are_scoped_to_a_code_change_and_head_sha() -> None:
    table: Table = CodeChangeDiff.__table__  # type: ignore[assignment]

    assert table.name == "code_change_diffs"
    assert {"code_change_id", "head_sha", "filename", "patch"} <= set(table.c.keys())
    assert any(
        {column.name for column in constraint.columns} == {"code_change_id", "head_sha", "filename"}
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
