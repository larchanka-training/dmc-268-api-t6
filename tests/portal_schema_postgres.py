"""What PostgreSQL test files share when they need only a migrated portal schema.

The ``portal_env`` fixture skips without ``TEST_DATABASE_URL`` and yields a migrated portal
schema, with no RabbitMQ. A test file loads it with
``pytest_plugins = ("tests.portal_schema_postgres",)``. The name is not ``env`` because plugins
are global and ``tests.worker_queue_postgres`` already defines an ``env`` that needs RabbitMQ.

The other test files whose local ``env`` only does this move to it at their next edit
(``.agents/rules/backend.md`` §5).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.portal_postgres import Env, portal_schema


@pytest.fixture
def portal_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema
