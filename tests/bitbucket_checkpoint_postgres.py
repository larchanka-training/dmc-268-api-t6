"""Isolated PostgreSQL database and repository for Bitbucket index checkpoint tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from tests.template_database import migrated_connection


@pytest.fixture
def code_index_database() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_bitbucket_checkpoint_{uuid4().hex}"
    with migrated_connection(database_url, schema) as connection:
        yield connection


@pytest.fixture
def checkpoint_repository(code_index_database: Connection) -> UUID:
    connection = code_index_database
    workspace_id, installation_id, repository_id = uuid4(), uuid4(), uuid4()
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', 17, '{\"account_login\": \"octo\"}')"
        ),
        {"id": installation_id, "workspace": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, 101, 'octo/repo', 'main', "
            "'https://github.com/octo/repo')"
        ),
        {"id": repository_id, "installation": installation_id},
    )
    return repository_id
