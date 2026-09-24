import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import (
    Engine,
    FindingCategory,
    FindingSeverity,
    FindingSide,
    RunState,
)
from app.modules.reviews.application.list_runs import RunCursor
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def one_or_none(self) -> tuple[Any, ...] | None:
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None


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


def test_sqlalchemy_run_repository_gets_detail_with_one_summary_query() -> None:
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
    session = FakeSession([(run, code_change, "org/repo", None, 4)])
    repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(session))
    )

    item = asyncio.run(repository.get_run(run.id))

    assert item is not None
    assert item.model is None
    assert item.action_count == 4
    assert session.statement is not None
    compiled = session.statement.compile()
    sql = str(compiled)
    assert "WHERE runs.id =" in sql
    assert "SELECT usage_events.model" in sql
    assert "SELECT count(*)" in sql
    assert run.id in compiled.params.values()


def test_sqlalchemy_run_repository_returns_only_published_comments_with_side_mapping() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    right_finding = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        file_path="app/service.py",
        line_start=23,
        side=FindingSide.RIGHT,
        severity=FindingSeverity.HIGH,
        category=FindingCategory.CORRECTNESS,
        confidence=Decimal("0.90"),
        title="Incorrect transition",
        body="Validation is skipped.",
        suggestion=None,
        rule_name="state-machine",
    )
    left_finding = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000003"),
        file_path="app/service.py",
        line_start=17,
        side=FindingSide.LEFT,
        severity=FindingSeverity.MEDIUM,
        category=FindingCategory.READABILITY,
        confidence=Decimal("0.75"),
        title="Old code",
        body="Remove this branch.",
        suggestion="",
        rule_name=None,
    )
    session = FakeSession([(run_id, right_finding), (run_id, left_finding)])
    repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(session))
    )

    comments = asyncio.run(repository.get_published_comments(run_id))

    assert comments is not None
    assert [(comment.old_line, comment.new_line) for comment in comments] == [
        (None, 23),
        (17, None),
    ]
    assert comments[0].rule_name == "state-machine"
    assert comments[1].rule_name is None
    assert session.statement is not None
    sql = str(session.statement.compile())
    assert "LEFT OUTER JOIN findings" in sql
    assert "findings.published IS true" in sql
    assert "WHERE runs.id =" in sql


def test_sqlalchemy_run_repository_returns_empty_comments_for_an_existing_run() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    session = FakeSession([(run_id, None)])
    repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(session))
    )

    comments = asyncio.run(repository.get_published_comments(run_id))

    assert comments == []


def test_sqlalchemy_run_repository_projects_ordered_actions_and_full_response() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    started_at = datetime(2026, 9, 24, tzinfo=UTC)
    action = SimpleNamespace(
        index=3,
        tool="github.get_file",
        request={"path": "app/service.py"},
        response={"content": "small"},
        response_ref=None,
        started_at=started_at,
        duration_ms=42,
    )
    actions_session = FakeSession([(run_id, action)])
    actions_repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(actions_session))
    )

    actions = asyncio.run(actions_repository.get_run_actions(run_id))

    assert actions is not None
    assert actions[0].index == 3
    assert actions[0].response == {"content": "small"}
    assert actions_session.statement is not None
    actions_sql = str(actions_session.statement.compile())
    assert "LEFT OUTER JOIN run_actions" in actions_sql
    assert "WHERE runs.id =" in actions_sql
    assert "ORDER BY run_actions.index ASC" in actions_sql

    response_session = FakeSession([({"content": "complete"},)])
    response_repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(response_session))
    )

    response = asyncio.run(response_repository.get_run_action_response(run_id, 3))

    assert response is not None
    assert response.response == {"content": "complete"}
    assert response_session.statement is not None
    response_sql = str(response_session.statement.compile())
    assert "FROM run_actions JOIN runs" in response_sql
    assert "run_actions.index =" in response_sql
    assert run_id in response_session.statement.compile().params.values()
    assert 3 in response_session.statement.compile().params.values()


def test_sqlalchemy_run_repository_returns_empty_actions_for_existing_run() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    session = FakeSession([(run_id, None)])
    repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(session))
    )

    actions = asyncio.run(repository.get_run_actions(run_id))

    assert actions == []


def test_sqlalchemy_run_repository_reads_snapshots_for_the_run_head_sha() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    session = FakeSession(
        [
            (run_id, "app/service.py", "diff --git a/app/service.py b/app/service.py"),
            (run_id, "generated.lock", None),
        ]
    )
    repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(session))
    )

    snapshots = asyncio.run(repository.get_run_diff(run_id))

    assert snapshots is not None
    assert [(snapshot.filename, snapshot.patch) for snapshot in snapshots] == [
        ("app/service.py", "diff --git a/app/service.py b/app/service.py"),
        ("generated.lock", None),
    ]
    assert session.statement is not None
    sql = str(session.statement.compile())
    assert "LEFT OUTER JOIN code_change_diffs" in sql
    assert "code_change_diffs.head_sha = runs.head_sha" in sql
    assert "ORDER BY code_change_diffs.filename ASC" in sql


def test_sqlalchemy_run_repository_reads_the_durable_diff_input_for_processing() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    code_change_id = UUID("00000000-0000-0000-0000-000000000002")
    session = FakeSession([(code_change_id, "a" * 40)])
    repository = SqlAlchemyRunRepository(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(session))
    )

    run_input = asyncio.run(repository.get_run_diff_input(run_id))

    assert run_input is not None
    assert run_input.code_change_id == code_change_id
    assert run_input.head_sha == "a" * 40
    assert session.statement is not None
    sql = str(session.statement.compile())
    assert "SELECT runs.code_change_id, runs.head_sha" in sql
    assert "WHERE runs.id =" in sql
