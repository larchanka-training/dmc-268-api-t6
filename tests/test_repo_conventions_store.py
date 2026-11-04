"""SQLAlchemy store of repository conventions: the cache row, the trace, the transaction."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.common.infrastructure.db.enums import CodeChangeState, Engine, RunState
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    RepoConventions,
    Repository,
    RuleVersion,
)
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    ConventionsFile,
)
from app.modules.reviews.application.prompt_builder import ReviewRule
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsStore,
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.models import CodeChange, PromptVersion, Run, RunAction
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.workspaces.infrastructure.models import Workspace

REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000501")
PROMPT_VERSION_ID = UUID("00000000-0000-0000-0000-000000000502")
RUN_ID = UUID("00000000-0000-0000-0000-000000000503")
SECOND_RUN_ID = UUID("00000000-0000-0000-0000-000000000504")
AGENTS_SHA = "a" * 40


@pytest.fixture
def migrated_conventions_database() -> Iterator[tuple[str, str]]:
    """Provide a disposable migrated PostgreSQL schema when explicitly configured."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_repo_conventions_{uuid4().hex}"
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
def test_sqlalchemy_store_returns_cached_conventions_for_repeated_same_key(
    migrated_conventions_database: tuple[str, str],
) -> None:
    database_url, schema = migrated_conventions_database
    expected = CachedConventions(
        repository_id=REPOSITORY_ID,
        agents_md_sha=AGENTS_SHA,
        prompt_version_id=PROMPT_VERSION_ID,
        key_patterns=("One.", "Two.", "Three."),
        recommendations=("One.", "Two.", "Three.", "Four.", "Five."),
        languages={"Python": 100},
    )

    async def seed_and_reuse_cache() -> tuple[CachedConventions, int, list[UUID]]:
        engine = create_async_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                workspace_id = uuid4()
                installation_id = uuid4()
                rule_version_id = uuid4()
                code_change_id = uuid4()
                workspace = Workspace(
                    id=workspace_id,
                    name="conventions",
                    daily_budget_usd=Decimal("1"),
                )
                installation = ProviderInstallation(
                    id=installation_id,
                    workspace_id=workspace_id,
                    provider="github",
                    external_id=1,
                    provider_metadata={},
                )
                repository = Repository(
                    id=REPOSITORY_ID,
                    provider_installation_id=installation_id,
                    external_id=1,
                    full_name="owner/repository",
                    default_branch="main",
                    web_url="https://example.test/owner/repository",
                )
                system_prompt = PromptVersion(
                    id=UUID("00000000-0000-0000-0000-000000000505"),
                    key="review.system",
                    version=1,
                    content="system prompt",
                    checksum="a" * 64,
                    is_active=True,
                )
                conventions_v1 = PromptVersion(
                    id=PROMPT_VERSION_ID,
                    key="review.conventions",
                    version=1,
                    content="conventions v1",
                    checksum="b" * 64,
                    is_active=False,
                )
                conventions_v2 = PromptVersion(
                    id=UUID("00000000-0000-0000-0000-000000000506"),
                    key="review.conventions",
                    version=2,
                    content="conventions v2",
                    checksum="9" * 64,
                    is_active=True,
                )
                rule_version = RuleVersion(
                    id=rule_version_id,
                    repository_id=REPOSITORY_ID,
                    version=1,
                    rules=[
                        {
                            "name": "Database boundary",
                            "include": ["app/**/*.py"],
                            "exclude": ["tests/**"],
                            "checks": ["Use cases commit once."],
                        }
                    ],
                    checksum="c" * 64,
                )
                code_change = CodeChange(
                    id=code_change_id,
                    repository_id=REPOSITORY_ID,
                    external_id=1,
                    external_number=1,
                    title="Cache hit",
                    description=None,
                    source_branch="feature/cache-hit",
                    target_branch="main",
                    base_sha="d" * 40,
                    head_sha="e" * 40,
                    state=CodeChangeState.OPEN,
                    web_url="https://example.test/owner/repository/pull/1",
                )
                now = datetime.now(UTC)
                runs = (
                    Run(
                        id=RUN_ID,
                        code_change_id=code_change_id,
                        base_sha="d" * 40,
                        head_sha="e" * 40,
                        state=RunState.SUCCEEDED,
                        trigger="manual",
                        idempotency_key="f" * 64,
                        engine=Engine.FAST,
                        rule_version_id=rule_version_id,
                        prompt_version_id=system_prompt.id,
                        available_at=now,
                        attempt=1,
                    ),
                    Run(
                        id=SECOND_RUN_ID,
                        code_change_id=code_change_id,
                        base_sha="d" * 40,
                        head_sha="e" * 40,
                        state=RunState.SUCCEEDED,
                        trigger="manual",
                        idempotency_key="0" * 64,
                        engine=Engine.FAST,
                        rule_version_id=rule_version_id,
                        prompt_version_id=system_prompt.id,
                        available_at=now,
                        attempt=1,
                    ),
                )
                session.add(workspace)
                await session.flush()
                session.add(installation)
                await session.flush()
                session.add_all((repository, system_prompt, conventions_v1, conventions_v2))
                await session.flush()
                session.add_all((rule_version, code_change))
                await session.flush()
                session.add_all(runs)
                await session.commit()

            conventions_input = await SqlAlchemyRunRepository(
                session_factory, allow_unscoped=True
            ).get_run_conventions_input(RUN_ID)
            assert conventions_input is not None
            assert conventions_input.conventions_prompt == ActiveConventionsPrompt(
                conventions_v2.id, "conventions v2"
            )
            assert conventions_input.rules == (
                ReviewRule(
                    name="Database boundary",
                    include=("app/**/*.py",),
                    exclude=("tests/**",),
                    checks=("Use cases commit once.",),
                ),
            )

            async with session_factory() as session:
                store = SqlAlchemyRepositoryConventionsStore(session)
                await store.save_and_record_trace(
                    RUN_ID,
                    expected,
                    (ConventionsFile(path="first.py", relevance="First pull request."),),
                )
                await session.commit()

            async with session_factory() as session:
                store = SqlAlchemyRepositoryConventionsStore(session)
                reused = await store.save_and_record_trace(
                    SECOND_RUN_ID,
                    expected,
                    (ConventionsFile(path="second.py", relevance="Second pull request."),),
                )
                await session.commit()
                convention_count = await session.scalar(select(func.count(RepoConventions.id)))
                trace_run_ids = list(
                    await session.scalars(select(RunAction.run_id).order_by(RunAction.run_id))
                )
                assert convention_count is not None
                return reused, convention_count, trace_run_ids
        finally:
            await engine.dispose()

    assert asyncio.run(seed_and_reuse_cache()) == (expected, 1, [RUN_ID, SECOND_RUN_ID])


class EmptyResult:
    def scalar_one_or_none(self) -> None:
        return None


class FakeSqlAlchemySession:
    def __init__(self) -> None:
        self.rows: list[object] = []
        self.flushes = 0

    async def execute(self, statement: object) -> EmptyResult:
        return EmptyResult()

    async def scalar(self, statement: object) -> int:
        text = str(statement)
        if "attempt" in text:
            return 1
        return -1

    def add(self, row: object) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        self.flushes += 1
        for row in self.rows:
            if isinstance(row, RepoConventions) and row.id is None:
                row.id = REPOSITORY_ID


def test_sqlalchemy_store_flushes_cache_and_current_run_trace_without_committing() -> None:
    session = FakeSqlAlchemySession()
    conventions = CachedConventions(
        repository_id=REPOSITORY_ID,
        agents_md_sha=AGENTS_SHA,
        prompt_version_id=PROMPT_VERSION_ID,
        key_patterns=("One.", "Two.", "Three."),
        recommendations=("One.", "Two.", "Three.", "Four.", "Five."),
        languages={"Python": 100},
    )

    result = asyncio.run(
        SqlAlchemyRepositoryConventionsStore(session).save_and_record_trace(  # type: ignore[arg-type]
            RUN_ID,
            conventions,
            (ConventionsFile(path="app/main.py", relevance="Entry point."),),
        )
    )

    assert result == conventions
    assert session.flushes == 2
    assert [type(row) for row in session.rows] == [RepoConventions, RunAction]
    trace = session.rows[-1]
    assert isinstance(trace, RunAction)
    assert trace.response == {"files": [{"path": "app/main.py", "relevance": "Entry point."}]}


class FailingTransactionSession(FakeSqlAlchemySession):
    def __init__(self) -> None:
        super().__init__()
        self.rollbacks = 0
        self.closed = False

    async def flush(self) -> None:
        await super().flush()
        if self.flushes == 2:
            raise RuntimeError("late trace flush failed")

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.rows.clear()

    async def close(self) -> None:
        self.closed = True


def test_real_uow_rolls_back_cache_draft_and_trace_after_late_flush_failure() -> None:
    session = FailingTransactionSession()
    conventions = CachedConventions(
        repository_id=REPOSITORY_ID,
        agents_md_sha=AGENTS_SHA,
        prompt_version_id=PROMPT_VERSION_ID,
        key_patterns=("One.", "Two.", "Three."),
        recommendations=("One.", "Two.", "Three.", "Four.", "Five."),
        languages={"Python": 100},
    )

    async def exercise() -> None:
        async with SqlAlchemyRepositoryConventionsUnitOfWork(lambda: session) as uow:  # type: ignore[arg-type]
            await uow.conventions.save_and_record_trace(
                RUN_ID,
                conventions,
                (ConventionsFile(path="app/main.py", relevance="Entry point."),),
            )
            await uow.commit()

    with pytest.raises(RuntimeError, match="late trace flush failed"):
        asyncio.run(exercise())

    assert session.rollbacks == 1
    assert session.rows == []
    assert session.closed is True
