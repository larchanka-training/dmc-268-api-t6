"""Shared migrated PostgreSQL fixture for agent access tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.portal_postgres import Env, portal_schema


@pytest.fixture
def agent_schema() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env
