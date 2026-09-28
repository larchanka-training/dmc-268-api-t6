"""Opt-in PostgreSQL proof for hashed refresh sessions and profile persistence."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.auth.application.exchange_github_code import AuthenticatedUser
from app.modules.auth.infrastructure.models import AuthRefreshSession, GitHubUserProfile
from app.modules.auth.infrastructure.sessions import SqlAlchemyAuthSessionUnitOfWork


@pytest.fixture
def auth_schema() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_auth_session_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_auth_session_uow_persists_profile_and_hash_only(
    auth_schema: tuple[str, str],
) -> None:
    database_url, schema = auth_schema
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    async def exercise() -> None:
        async with SqlAlchemyAuthSessionUnitOfWork(factory) as uow:
            await uow.sessions.create(
                AuthenticatedUser(42, "octocat", "Octo Cat", "https://github.test/avatar.png"),
                "a" * 64,
                uuid4(),
                expires_at,
            )
            await uow.commit()
        async with factory() as session:
            profile = await session.scalar(
                select(GitHubUserProfile).where(GitHubUserProfile.id == 42)
            )
            refresh = await session.scalar(select(AuthRefreshSession))
            assert profile is not None
            assert (profile.login, profile.name, profile.avatar_url) == (
                "octocat",
                "Octo Cat",
                "https://github.test/avatar.png",
            )
            assert refresh is not None
            assert refresh.github_user_id == 42
            assert refresh.token_hash == "a" * 64
            assert refresh.expires_at == expires_at
            assert "ghu_" not in str(profile.__dict__) + str(refresh.__dict__)

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())
