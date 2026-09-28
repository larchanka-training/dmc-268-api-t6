"""Opt-in PostgreSQL migration and family-lock replay proof."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.auth.application.refresh_session import (
    InvalidRefreshToken,
    LogoutLocalSession,
    RefreshLocalSession,
)
from app.modules.auth.infrastructure.models import AuthRefreshFamily, AuthRefreshSession
from app.modules.auth.infrastructure.sessions import SqlAlchemyAuthSessionUnitOfWork


class FakeIssuer:
    def issue(self, user_id: int, workspace_ids: tuple[UUID, ...]) -> str:
        assert user_id == 42 and workspace_ids == ()
        return "signed-access-token"


@pytest.fixture
def migrated_auth_family() -> Iterator[tuple[str, str, UUID]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_auth_refresh_{uuid4().hex}"
    family_id = uuid4()
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "20260928_0017")
            expires_at = datetime.now(UTC) + timedelta(days=30)
            connection.execute(
                text(
                    "INSERT INTO github_user_profiles (id, login, name) "
                    "VALUES (42, 'octocat', 'Octo Cat')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO auth_refresh_sessions "
                    "(id, family_id, github_user_id, token_hash, expires_at) "
                    "VALUES (:id, :family, 42, :hash, :expires)"
                ),
                {
                    "id": uuid4(),
                    "family": family_id,
                    "hash": hashlib.sha256(b"original-secret").hexdigest(),
                    "expires": expires_at,
                },
            )
            connection.commit()
            command.upgrade(config, "head")
            backfilled = connection.execute(
                text(
                    "SELECT github_user_id, expires_at, revoked_at "
                    "FROM auth_refresh_families WHERE id = :id"
                ),
                {"id": family_id},
            ).one()
            assert backfilled[0] == 42
            assert backfilled[1] == expires_at
            assert backfilled[2] is None
            yield database_url, schema, family_id
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_concurrent_refresh_replay_revokes_every_family_token(
    migrated_auth_family: tuple[str, str, UUID],
) -> None:
    database_url, schema, family_id = migrated_auth_family
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    refresh = RefreshLocalSession(
        uow_factory=lambda: SqlAlchemyAuthSessionUnitOfWork(factory),
        issuer=FakeIssuer(),
        new_refresh_token=lambda: "replacement-secret",
    )

    async def exercise() -> None:
        outcomes = await asyncio.gather(
            refresh.execute("original-secret"),
            refresh.execute("original-secret"),
            return_exceptions=True,
        )
        assert sum(isinstance(item, InvalidRefreshToken) for item in outcomes) == 1
        assert (
            sum(getattr(item, "refresh_token", None) == "replacement-secret" for item in outcomes)
            == 1
        )
        async with factory() as session:
            family = await session.scalar(
                select(AuthRefreshFamily).where(AuthRefreshFamily.id == family_id)
            )
            rows = (
                await session.scalars(
                    select(AuthRefreshSession).where(AuthRefreshSession.family_id == family_id)
                )
            ).all()
            assert family is not None and family.revoked_at is not None
            assert len(rows) == 2 and all(row.revoked_at is not None for row in rows)
            assert sum(row.rotated_at is not None for row in rows) == 1
        with pytest.raises(InvalidRefreshToken):
            await refresh.execute("replacement-secret")

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_logout_revokes_migrated_family_and_is_idempotent(
    migrated_auth_family: tuple[str, str, UUID],
) -> None:
    database_url, schema, family_id = migrated_auth_family
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    logout = LogoutLocalSession(uow_factory=lambda: SqlAlchemyAuthSessionUnitOfWork(factory))

    async def exercise() -> None:
        await logout.execute("original-secret")
        await logout.execute("original-secret")
        async with factory() as session:
            family = await session.scalar(
                select(AuthRefreshFamily).where(AuthRefreshFamily.id == family_id)
            )
            assert family is not None and family.revoked_at is not None

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())
