"""Durable T6 cancellation signal for attempted queued Runs."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.sql import ClauseElement

from alembic import command
from app.common.infrastructure.db.enums import Engine, RunState
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionUnitOfWork,
    SqlAlchemyPullRequestRunCanceller,
)
from app.modules.reviews.infrastructure.models import Run

_PR = UUID("11111111-1111-1111-1111-111111111111")
_WORKSPACE = UUID("22222222-2222-2222-2222-222222222222")
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self, runs: list[Run]) -> None:
        self.runs = runs
        self.flushes = 0

    async def scalar(self, statement: object) -> UUID:
        return _WORKSPACE

    async def scalars(self, statement: object) -> FakeSession:
        return self

    def all(self) -> list[Run]:
        return self.runs

    async def flush(self) -> None:
        self.flushes += 1


@pytest.mark.parametrize("reason", ["superseded", "pr_closed", "label_removed"])
def test_attempted_queued_cancellation_is_durable_and_duplicate_is_idempotent(
    reason: str,
) -> None:
    fresh = Run(id=UUID(int=1), state=RunState.QUEUED, attempt=0)
    attempted = Run(id=UUID(int=2), state=RunState.QUEUED, attempt=2)
    session = FakeSession([fresh, attempted])
    store = SqlAlchemyPullRequestRunCanceller(cast(AsyncSession, session))

    first = asyncio.run(store.cancel_for_pr(_PR, reason, _NOW))

    assert [notice.run_id for notice in first] == [fresh.id, attempted.id]
    assert fresh.state == attempted.state == RunState.CANCELLED
    assert fresh.cancellation_signal_requested_at is None
    assert attempted.cancellation_signal_requested_at == _NOW
    assert attempted.cancellation_signal_published_at is None

    second = asyncio.run(store.cancel_for_pr(_PR, reason, _NOW + timedelta(minutes=1)))
    assert second == ()
    assert attempted.cancellation_signal_requested_at == _NOW
    assert session.flushes == 2


def test_pending_cancellation_query_rebuilds_old_head_pointer_without_queued_filters() -> None:
    run = Run(
        id=UUID(int=20),
        code_change_id=_PR,
        base_sha="b" * 40,
        base_ref="main",
        head_sha="a" * 40,
        engine=Engine.FAST,
        rule_version_id=UUID(int=21),
        prompt_version_id=UUID(int=22),
        attempt=2,
        cancellation_signal_requested_at=_NOW,
    )
    pr = SimpleNamespace(external_number=7, target_branch="main")
    repository = SimpleNamespace(id=UUID(int=23), external_id=101, full_name="octo/repo")
    installation = SimpleNamespace(workspace_id=_WORKSPACE, external_id=17)

    class QuerySession:
        statement: ClauseElement | None = None

        async def execute(self, statement: ClauseElement) -> QuerySession:
            self.statement = statement
            return self

        def all(self) -> list[tuple[Run, object, object, object]]:
            return [(run, pr, repository, installation)]

    session = QuerySession()
    store = SqlAlchemyPullRequestRunCanceller(cast(AsyncSession, session))

    messages = asyncio.run(store.pending_cancellation_signals(10))

    assert len(messages) == 1
    assert messages[0].run_id == run.id
    assert messages[0].workspace_id == _WORKSPACE
    assert messages[0].head_sha == "a" * 40
    assert messages[0].attempt == 3
    assert messages[0].requested_at == _NOW
    assert session.statement is not None
    sql = str(session.statement)
    predicate = sql.split("WHERE", 1)[1]
    assert "runs.cancellation_signal_requested_at IS NOT NULL" in predicate
    assert "runs.cancellation_signal_published_at IS NULL" in predicate
    assert "code_changes.head_sha" not in predicate
    assert "code_changes.ai_review_labeled" not in predicate


@pytest.fixture
def cancellation_database() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_cancel_signal_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            workspace_id = UUID(int=10)
            installation_id = UUID(int=11)
            repository_id = UUID(int=12)
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, daily_budget_usd) "
                    "VALUES (:id, 'Cancellation signal test', 0)"
                ),
                {"id": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :workspace_id, 'github', 17, '{}'::jsonb)"
                ),
                {"id": installation_id, "workspace_id": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO repositories "
                    "(id, provider_installation_id, external_id, full_name, "
                    "default_branch, web_url) "
                    "VALUES (:id, :installation_id, 101, 'octo/repo', 'main', "
                    "'https://github.com/octo/repo')"
                ),
                {"id": repository_id, "installation_id": installation_id},
            )
            connection.execute(
                text(
                    "INSERT INTO code_changes "
                    "(id, repository_id, external_id, external_number, title, source_branch, "
                    "target_branch, base_sha, head_sha, state, web_url) "
                    "VALUES (:id, :repository_id, 901, 7, 'Test PR', 'feature', 'main', "
                    ":base_sha, :head_sha, 'closed', 'https://github.com/octo/repo/pull/7')"
                ),
                {
                    "id": _PR,
                    "repository_id": repository_id,
                    "base_sha": "b" * 40,
                    "head_sha": "c" * 40,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                    "VALUES (:id, 'review.system', 1, 'system', :checksum, true)"
                ),
                {"id": UUID(int=13), "checksum": "a" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO rule_versions "
                    "(id, repository_id, version, rules, checksum, is_active) "
                    "VALUES (:id, :repository_id, 1, '[]'::jsonb, :checksum, true)"
                ),
                {"id": UUID(int=14), "repository_id": repository_id, "checksum": "b" * 64},
            )
            connection.commit()
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_postgres_cancelled_run_signal_replays_old_head_until_confirmed(
    cancellation_database: tuple[str, str],
) -> None:
    database_url, schema = cancellation_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        run_id = UUID(int=15)
        try:
            async with sessions() as session:
                session.add(
                    Run(
                        id=run_id,
                        code_change_id=_PR,
                        base_sha="b" * 40,
                        base_ref="main",
                        head_sha="a" * 40,
                        state=RunState.QUEUED,
                        trigger="webhook",
                        idempotency_key="d" * 64,
                        engine=Engine.FAST,
                        rule_version_id=UUID(int=14),
                        prompt_version_id=UUID(int=13),
                        attempt=2,
                        available_at=_NOW,
                        created_at=_NOW,
                    )
                )
                await session.commit()

            async with SqlAlchemyPullRequestProjectionUnitOfWork(sessions) as uow:
                notices = await uow.runs.cancel_for_pr(_PR, "pr_closed", _NOW)
                for notice in notices:
                    await uow.runs.notify_run_updated(notice)
                await uow.commit()
            assert [notice.run_id for notice in notices] == [run_id]

            async with SqlAlchemyPullRequestProjectionUnitOfWork(sessions) as uow:
                messages = await uow.runs.pending_cancellation_signals(10)
            assert len(messages) == 1
            message = messages[0]
            assert message.run_id == run_id
            assert message.workspace_id == UUID(int=10)
            assert message.head_sha == "a" * 40  # Current PR head is different.
            assert message.attempt == 3
            assert message.requested_at == _NOW

            async with SqlAlchemyPullRequestProjectionUnitOfWork(sessions) as uow:
                await uow.runs.mark_cancellation_signal_published(run_id, _NOW)
                await uow.commit()
            async with SqlAlchemyPullRequestProjectionUnitOfWork(sessions) as uow:
                assert await uow.runs.pending_cancellation_signals(10) == ()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
