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
                assert column.type.name is not None and column.type.enum_class is not None
                labels.setdefault(column.type.name, set()).add(
                    tuple(member.value for member in column.type.enum_class)
                )
    return labels


def _postgres_labels(connection: Connection) -> dict[str, tuple[str, ...]]:
    names = connection.scalars(
        text(
            "SELECT typname FROM pg_type "
            "WHERE typtype = 'e' AND typnamespace = current_schema()::regnamespace"
        )
    ).all()
    return {
        name: tuple(connection.scalar(text(f"SELECT enum_range(NULL::{name})::text[]")))
        for name in names
    }


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
