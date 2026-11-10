"""Public Bitbucket pages read only active bindings in the selected workspace."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bootstrap.bitbucket import get_bitbucket_management
from app.bootstrap.portal_auth import get_auth_scope
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketCredentialCodec,
    BitbucketRepositoryProvider,
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketManagementUnitOfWork,
)
from tests.bitbucket_connection_postgres_support import (
    PublicConnectionPageSeed,
    seed_public_connection_pages,
)

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@dataclass(frozen=True)
class PublicConnectionPageCase:
    db: Connection
    client: TestClient
    seed: PublicConnectionPageSeed

    @property
    def url(self) -> str:
        return f"/api/workspaces/{self.seed.workspace}/bitbucket/connections"


@pytest.fixture
def public_connection_page_case(
    bitbucket_connection_schema: Connection,
) -> Iterator[PublicConnectionPageCase]:
    db = bitbucket_connection_schema
    seed = seed_public_connection_pages(db)
    schema = str(db.scalar(text("SELECT current_schema()")))
    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    manager = ManageBitbucketConnection(
        uow_factory=lambda: SqlAlchemyBitbucketManagementUnitOfWork(factory),
        cipher=cast(BitbucketCredentialCodec, object()),
        provider=cast(BitbucketRepositoryProvider, object()),
        rule_sets={},
        webhook_base_url="https://portal.test",
    )
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(
        42, (seed.workspace, seed.foreign_workspace)
    )
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        yield PublicConnectionPageCase(db, TestClient(app), seed)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_public_connection_pages_are_bounded_and_ordered(
    public_connection_page_case: PublicConnectionPageCase,
) -> None:
    case = public_connection_page_case
    first = case.client.get(case.url)
    assert first.status_code == 200
    assert [row["repositorySlug"] for row in first.json()["connections"]] == [
        f"repo{index:03d}" for index in range(100)
    ]
    cursor = first.json()["nextCursor"]
    assert isinstance(cursor, str) and cursor

    second = case.client.get(case.url, params={"cursor": cursor})
    assert second.status_code == 200
    assert [row["repositorySlug"] for row in second.json()["connections"]] == [
        "repo100",
        "repo101",
    ]
    assert second.json()["nextCursor"] is None


@pytest.mark.integration
def test_public_connection_cursor_rejects_foreign_workspace_and_nonmember(
    public_connection_page_case: PublicConnectionPageCase,
) -> None:
    case = public_connection_page_case
    first = case.client.get(case.url)
    assert first.status_code == 200
    cursor = first.json()["nextCursor"]
    assert isinstance(cursor, str) and cursor

    foreign = case.client.get(
        f"/api/workspaces/{case.seed.foreign_workspace}/bitbucket/connections",
        params={"cursor": cursor},
    )
    assert (foreign.status_code, foreign.json()) == (400, {"detail": "invalid_cursor"})
    not_member = case.client.get(f"/api/workspaces/{UUID(int=999)}/bitbucket/connections")
    assert not_member.status_code == 404


@pytest.mark.integration
def test_public_connection_cursor_returns_empty_page_after_remaining_rows_disconnect(
    public_connection_page_case: PublicConnectionPageCase,
) -> None:
    case = public_connection_page_case
    first = case.client.get(case.url)
    assert first.status_code == 200
    cursor = first.json()["nextCursor"]
    assert isinstance(cursor, str) and cursor

    case.db.execute(
        text(
            "UPDATE repositories SET installation_available=false "
            "WHERE provider_installation_id=:installation "
            "AND full_name IN ('team/repo100', 'team/repo101')"
        ),
        {"installation": case.seed.installation},
    )
    case.db.commit()
    empty = case.client.get(case.url, params={"cursor": cursor})
    assert empty.status_code == 200
    assert empty.json() == {"connections": [], "nextCursor": None}
