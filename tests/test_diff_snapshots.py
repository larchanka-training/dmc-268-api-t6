from sqlalchemy import Table, UniqueConstraint

from app.modules.reviews.infrastructure.models import CodeChangeDiff


def test_diff_snapshots_are_scoped_to_one_run() -> None:
    table: Table = CodeChangeDiff.__table__  # type: ignore[assignment]

    assert table.name == "code_change_diffs"
    assert {"run_id", "code_change_id", "head_sha", "filename", "patch"} <= set(table.c.keys())
    assert any(
        {column.name for column in constraint.columns} == {"run_id", "filename"}
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
