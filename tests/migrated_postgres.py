"""The ``migrated_db`` fixture: a database of the test's own at alembic head (#282).

A test file loads it with ``pytest_plugins = ("tests.migrated_postgres",)`` and seeds or
connects to ``migrated_db.url`` with ``search_path`` on ``migrated_db.schema``. It skips without
``TEST_DATABASE_URL``. The database is a clone of ``tests/template_database.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest

from tests.template_database import MigratedDatabase, migrated_database


@pytest.fixture
def migrated_db() -> Iterator[MigratedDatabase]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_{uuid4().hex}"
    with migrated_database(database_url, schema) as url:
        yield MigratedDatabase(url, schema)
