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
from app.modules.auth.application.exchange_github_code import ExchangedSession
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
    clock = [datetime.now(UTC)]
    token_index = 0

    def next_token() -> str:
        nonlocal token_index
        token_index += 1
        return f"replacement-secret-{token_index}"

    refresh = RefreshLocalSession(
        uow_factory=lambda: SqlAlchemyAuthSessionUnitOfWork(factory),
        issuer=FakeIssuer(),
        new_refresh_token=next_token,
        now=lambda: clock[0],
    )

    async def exercise() -> None:
        outcomes = await asyncio.gather(
            refresh.execute("original-secret"),
            refresh.execute("original-secret"),
            return_exceptions=True,
        )
        assert all(isinstance(item, ExchangedSession) for item in outcomes)
        async with factory() as session:
            family = await session.scalar(
                select(AuthRefreshFamily).where(AuthRefreshFamily.id == family_id)
            )
            rows = (
                await session.scalars(
                    select(AuthRefreshSession).where(AuthRefreshSession.family_id == family_id)
                )
            ).all()
            assert family is not None and family.revoked_at is None
            assert len(rows) == 3 and all(row.revoked_at is None for row in rows)
            assert sum(row.rotated_at is not None for row in rows) == 1

        # Replay outside the grace period revokes the entire family
        clock[0] += timedelta(seconds=16)
        with pytest.raises(InvalidRefreshToken):
            await refresh.execute("original-secret")

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
            assert len(rows) == 3 and all(row.revoked_at is not None for row in rows)

        with pytest.raises(InvalidRefreshToken):
            await refresh.execute("replacement-secret-1")

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


@pytest.mark.integration
def test_sequential_rotations_with_concurrent_refresh_postgres(
    migrated_auth_family: tuple[str, str, UUID],
) -> None:
    database_url, schema, family_id = migrated_auth_family
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime.now(UTC)]
    token_index = 0

    def next_token() -> str:
        nonlocal token_index
        token_index += 1
        return f"seq-replacement-{token_index}"

    refresh = RefreshLocalSession(
        uow_factory=lambda: SqlAlchemyAuthSessionUnitOfWork(factory),
        issuer=FakeIssuer(),
        new_refresh_token=next_token,
        now=lambda: clock[0],
    )

    async def exercise() -> None:
        # Perform 4 sequential rotations spaced 15 minutes apart
        current_token = "original-secret"
        for _i in range(1, 5):
            clock[0] += timedelta(minutes=15)
            res = await refresh.execute(current_token)
            current_token = res.refresh_token

        # Now current_token is seq-replacement-4.
        # seq-replacement-3 was rotated at t3 (last rotation).
        # Replay seq-replacement-3 concurrently within 15s window:
        # both replays must succeed because earlier rotations don't count towards the limit!
        clock[0] += timedelta(seconds=2)
        outcomes = await asyncio.gather(
            refresh.execute("seq-replacement-3"),
            refresh.execute("seq-replacement-3"),
            return_exceptions=True,
        )
        assert all(isinstance(item, ExchangedSession) for item in outcomes)
        async with factory() as session:
            family = await session.scalar(
                select(AuthRefreshFamily).where(AuthRefreshFamily.id == family_id)
            )
            assert family is not None and family.revoked_at is None

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_grace_limit_revocation_committed_to_postgres(
    migrated_auth_family: tuple[str, str, UUID],
) -> None:
    database_url, schema, family_id = migrated_auth_family
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime.now(UTC)]
    token_index = 0

    def next_token() -> str:
        nonlocal token_index
        token_index += 1
        return f"limit-token-{token_index}"

    refresh = RefreshLocalSession(
        uow_factory=lambda: SqlAlchemyAuthSessionUnitOfWork(factory),
        issuer=FakeIssuer(),
        new_refresh_token=next_token,
        now=lambda: clock[0],
    )

    async def exercise() -> None:
        # Initial rotation: creates limit-token-1
        await refresh.execute("original-secret")

        # 4 grace reissues: create limit-token-2..5
        for _ in range(4):
            await refresh.execute("original-secret")

        # 5th grace replay: count is 5 >= MAX_GRACE_REFRESH_SESSIONS (5) ->
        # revokes family, commits the revocation in Postgres, and raises InvalidRefreshToken
        with pytest.raises(InvalidRefreshToken):
            await refresh.execute("original-secret")

        # In a separate transaction, verify the revocation was durably committed to DB
        async with factory() as session:
            family = await session.scalar(
                select(AuthRefreshFamily).where(AuthRefreshFamily.id == family_id)
            )
            assert family is not None and family.revoked_at is not None

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.integration
@pytest.mark.parametrize("rotated", [False, True])
def test_legacy_extended_family_cannot_live_beyond_thirty_days_from_login(
    migrated_auth_family: tuple[str, str, UUID], rotated: bool
) -> None:
    from fastapi.testclient import TestClient

    from app.bootstrap.auth_api import get_refresh_local_session
    from app.main import app

    database_url, schema, family_id = migrated_auth_family
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 10, 28, 11, 59, 59, tzinfo=UTC)]
    refresh = RefreshLocalSession(
        uow_factory=lambda: SqlAlchemyAuthSessionUnitOfWork(factory),
        issuer=FakeIssuer(),
        new_refresh_token=lambda: "last-second-token",
        now=lambda: clock[0],
    )

    async def exercise() -> None:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE auth_refresh_families SET created_at=:login, expires_at=:extended "
                    "WHERE id=:family"
                ),
                {
                    "login": datetime(2026, 9, 28, 12, tzinfo=UTC),
                    "extended": datetime(2026, 11, 27, 12, tzinfo=UTC),
                    "family": family_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE auth_refresh_sessions SET expires_at=:extended, rotated_at=:rotated "
                    "WHERE family_id=:family"
                ),
                {
                    "extended": datetime(2026, 11, 27, 12, tzinfo=UTC),
                    "family": family_id,
                    "rotated": datetime(2026, 10, 28, 11, 59, 50, tzinfo=UTC) if rotated else None,
                },
            )
        assert (await refresh.execute("original-secret")).refresh_token == "last-second-token"
        async with factory() as session:
            replacement = await session.scalar(
                select(AuthRefreshSession).where(
                    AuthRefreshSession.token_hash
                    == hashlib.sha256(b"last-second-token").hexdigest()
                )
            )
            assert replacement is not None
            assert replacement.expires_at == datetime(2026, 10, 28, 12, tzinfo=UTC)
        clock[0] = datetime(2026, 10, 28, 12, tzinfo=UTC)
        with pytest.raises(InvalidRefreshToken):
            await refresh.execute("original-secret")

    try:
        asyncio.run(exercise())
        app.dependency_overrides[get_refresh_local_session] = lambda: refresh
        client = TestClient(app, base_url="https://testserver")
        assert (
            client.post(
                "/api/auth/refresh", headers={"Cookie": "refresh_token=last-second-token"}
            ).status_code
            == 401
        )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_repeated_normal_and_grace_rotations_preserve_persisted_login_deadline(
    migrated_auth_family: tuple[str, str, UUID],
) -> None:
    database_url, schema, family_id = migrated_auth_family
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 9, 28, 12, tzinfo=UTC)]
    tokens = iter(("day-one", "day-ten", "day-ten-grace", "last-day"))
    refresh = RefreshLocalSession(
        uow_factory=lambda: SqlAlchemyAuthSessionUnitOfWork(factory),
        issuer=FakeIssuer(),
        new_refresh_token=lambda: next(tokens),
        now=lambda: clock[0],
    )

    async def exercise() -> None:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE auth_refresh_families SET created_at=:login, expires_at=:deadline "
                    "WHERE id=:family"
                ),
                {
                    "login": datetime(2026, 9, 28, 12, tzinfo=UTC),
                    "deadline": datetime(2026, 10, 28, 12, tzinfo=UTC),
                    "family": family_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE auth_refresh_sessions SET expires_at=:deadline WHERE family_id=:family"
                ),
                {"deadline": datetime(2026, 10, 28, 12, tzinfo=UTC), "family": family_id},
            )
        for at, presented, expected in (
            (datetime(2026, 9, 29, 12, tzinfo=UTC), "original-secret", "day-one"),
            (datetime(2026, 10, 8, 12, tzinfo=UTC), "day-one", "day-ten"),
            (datetime(2026, 10, 8, 12, 0, 5, tzinfo=UTC), "day-one", "day-ten-grace"),
            (datetime(2026, 10, 27, 12, tzinfo=UTC), "day-ten-grace", "last-day"),
        ):
            clock[0] = at
            assert (await refresh.execute(presented)).refresh_token == expected
            async with factory() as session:
                family = await session.get(AuthRefreshFamily, family_id)
                assert family is not None
                assert family.expires_at == datetime(2026, 10, 28, 12, tzinfo=UTC)
                deadlines = (
                    await session.scalars(
                        select(AuthRefreshSession.expires_at).where(
                            AuthRefreshSession.family_id == family_id
                        )
                    )
                ).all()
                assert set(deadlines) == {datetime(2026, 10, 28, 12, tzinfo=UTC)}
        clock[0] = datetime(2026, 10, 28, 12, tzinfo=UTC)
        with pytest.raises(InvalidRefreshToken):
            await refresh.execute("last-day")

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())
