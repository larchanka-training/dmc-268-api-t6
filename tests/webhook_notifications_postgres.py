"""The database the webhook notification tests share: a clone with an async engine (#129).

A test file loads the ``database`` fixture with
``pytest_plugins = ("tests.webhook_notifications_postgres",)``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.template_database import MigratedDatabase

pytest_plugins = ("tests.migrated_postgres",)


@dataclass(frozen=True)
class Database:
    url: str
    schema: str

    def engine(self) -> AsyncEngine:
        return create_async_engine(
            self.url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )


@pytest.fixture
def database(migrated_db: MigratedDatabase) -> Database:
    return Database(migrated_db.url, migrated_db.schema)
