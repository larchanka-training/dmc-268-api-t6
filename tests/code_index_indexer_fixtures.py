"""Disposable PostgreSQL fixtures for the graph indexer tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.code_index_indexer_harness import _Database


def _repository(connection: Connection, installation_external_id: int, full_name: str) -> UUID:
    workspace_id, installation_id, repository_id = uuid4(), uuid4(), uuid4()
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', :external_id, '{\"account_login\": \"octo\"}')"
        ),
        {"id": installation_id, "workspace": workspace_id, "external_id": installation_external_id},
    )
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, 101, :full_name, 'main', 'https://github.com/x')"
        ),
        {"id": repository_id, "installation": installation_id, "full_name": full_name},
    )
    return repository_id


@pytest.fixture
def database(code_index_database: Connection) -> Iterator[_Database]:
    """Two repositories in their own installations, committed for async sessions."""
    connection = code_index_database
    repository_a = _repository(connection, 17, "octo/indexed-a")
    repository_b = _repository(connection, 18, "octo/indexed-b")
    connection.commit()
    schema = connection.scalar(text("SELECT current_schema()"))
    engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    try:
        yield _Database(
            async_sessionmaker(engine, expire_on_commit=False), repository_a, repository_b
        )
    finally:
        asyncio.run(engine.dispose())
