"""The ``migrated_db`` fixture: a database of the test's own at alembic head (#282).

A test file loads it with ``pytest_plugins = ("tests.migrated_postgres",)`` and seeds it through
``migrated_db.connect()`` or connects to ``migrated_db.url`` with ``search_path`` on
``migrated_db.schema``. The database is a clone of ``tests/template_database.py``. Both fixtures
skip without ``TEST_DATABASE_URL``; ``test_database_url`` is for tests of the clones themselves.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest

from tests.template_database import MigratedDatabase, migrated_database


@pytest.fixture
def test_database_url() -> str:
    """``TEST_DATABASE_URL``: the server the clones are made on."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    return url


@pytest.fixture
def migrated_db(test_database_url: str) -> Iterator[MigratedDatabase]:
    schema = f"test_{uuid4().hex}"
    with migrated_database(test_database_url, schema) as url:
        yield MigratedDatabase(url, schema)
