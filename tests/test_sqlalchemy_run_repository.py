import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import Engine, RunState
from app.modules.reviews.application.list_runs import RunCursor
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.statement: Select[Any] | None = None

    async def execute(self, statement: Select[Any]) -> FakeResult:
        self.statement = statement
        return FakeResult(self.rows)


class FakeSessionContext(AbstractAsyncContextManager[FakeSession]):
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self._session)


def test_sqlalchemy_run_repository_filters_and_orders_with_a_tied_timestamp_cursor() -> None:
    created_at = datetime(2026, 9, 24, tzinfo=UTC)
    run = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        state=RunState.SUCCEEDED,
        engine=Engine.FAST,
        attempt=1,
        cancel_requested=False,
        started_at=created_at,
        finished_at=None,
        error_code=None,
        head_sha="a" * 40,
        created_at=created_at,
    )
    code_change = SimpleNamespace(
        external_number=5, title="Review me", web_url="https://example.test/pull/5"
    )
    session = FakeSession([(run, code_change, "org/repo", "gpt-test", 3)])
    repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(session))
    )

    items = asyncio.run(
        repository.list_runs(
            status="succeeded",
            repository="org/repo",
            cursor=RunCursor(created_at=created_at, id=run.id),
            limit=2,
        )
    )

    assert items[0].repo == "org/repo"
    assert items[0].model == "gpt-test"
    assert items[0].action_count == 3
    assert items[0].head_sha == "a" * 40
    assert session.statement is not None
    compiled = session.statement.compile()
    sql = str(compiled)
    assert "runs.state =" in sql
    assert "repositories.full_name =" in sql
    assert "runs.created_at <" in sql
    assert "runs.created_at =" in sql
    assert "runs.id <" in sql
    assert "ORDER BY runs.created_at DESC, runs.id DESC" in sql
    assert "succeeded" in compiled.params.values()
    assert "org/repo" in compiled.params.values()
    assert list(compiled.params.values()).count(created_at) == 2
    assert run.id in compiled.params.values()
