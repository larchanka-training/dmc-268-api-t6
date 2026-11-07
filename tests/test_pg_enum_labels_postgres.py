"""PostgreSQL ENUM labels after ``upgrade head`` against the Python classes of the models (#524)."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, Enum, text

from app.bootstrap.db_metadata import metadata

pytest_plugins = ("tests.code_index_postgres",)


def _python_labels() -> dict[str, set[tuple[str, ...]]]:
    # Every model module is imported by db_metadata, as for Alembic: after `import app.main`
    # alone the billing models are missing and their types would drop out of the comparison.
    labels: dict[str, set[tuple[str, ...]]] = {}
    for table in metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, Enum):
                where = f"{table.name}.{column.name}"
                assert column.type.name is not None, where
                assert column.type.enum_class is not None, where
                labels.setdefault(column.type.name, set()).add(
                    tuple(member.value for member in column.type.enum_class)
                )
    return labels


def _postgres_labels(connection: Connection) -> dict[str, tuple[str, ...]]:
    rows = connection.execute(
        text(
            "SELECT t.typname, ARRAY("
            "SELECT e.enumlabel::text FROM pg_enum e "
            "WHERE e.enumtypid = t.oid ORDER BY e.enumsortorder) "
            "FROM pg_type t "
            "WHERE t.typtype = 'e' AND t.typnamespace = current_schema()::regnamespace"
        )
    ).all()
    return {name: tuple(labels) for name, labels in rows}


@pytest.mark.integration
def test_every_postgres_enum_has_the_labels_of_its_python_class(
    code_index_database: Connection,
) -> None:
    postgres = _postgres_labels(code_index_database)
    python = _python_labels()

    assert set(postgres) == set(python)
    mismatches = {
        name: (postgres[name], classes)
        for name, classes in python.items()
        if classes != {postgres[name]}
    }
    assert mismatches == {}
