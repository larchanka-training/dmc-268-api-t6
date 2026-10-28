"""Bitbucket Run authority is pinned independently of mutable installation state."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, inspect

from alembic import command
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)


@pytest.mark.integration
def test_bitbucket_run_origin_schema_has_full_immutable_snapshot(
    bitbucket_connection_schema: Connection,
) -> None:
    command.upgrade(_config(bitbucket_connection_schema), "head")
    columns = {
        item["name"]: item
        for item in inspect(bitbucket_connection_schema).get_columns("bitbucket_run_origins")
    }
    assert set(columns) == {
        "run_id",
        "workspace_id",
        "provider_installation_id",
        "repository_id",
        "repository_uuid",
        "binding_generation",
        "credential_generation",
        "pr_number",
        "head_sha",
        "run_order",
    }
    assert all(not item["nullable"] for item in columns.values())
