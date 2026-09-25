"""Behavioural contracts for cached repository conventions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from pydantic import ValidationError
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
    ConventionsDraft,
    ConventionsFile,
    GenerateRepoConventions,
    RepositoryFile,
    RepositorySnapshot,
    derive_languages,
)
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


def draft() -> dict[str, object]:
    return {
        "files": [{"path": "app/main.py", "relevance": "Application entrypoint."}],
        "key_patterns": ["Pattern one.", "Pattern two.", "Pattern three."],
        "recommendations": [
            "Recommendation one.",
            "Recommendation two.",
            "Recommendation three.",
            "Recommendation four.",
            "Recommendation five.",
        ],
    }


@dataclass
class FakeSource:
    calls: list[str] = field(default_factory=list)

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        assert repository_id == REPOSITORY_ID
        self.calls.append("agents")
        return RepositorySnapshot(content="Always use uv.", sha=AGENTS_SHA)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        assert repository_id == REPOSITORY_ID
        self.calls.append("tree")
        return (
            RepositoryFile("app/main.py", 60),
            RepositoryFile("web/app.ts", 40),
            RepositoryFile("README.md", 100),
        )

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        assert repository_id == REPOSITORY_ID
        self.calls.append(f"files:{','.join(paths)}")
        return tuple(RepositoryFile(path, 10, content=f"content for {path}") for path in paths)


@dataclass
class FakeModel:
    result: dict[str, object]
    calls: list[tuple[str | None, tuple[RepositoryFile, ...], dict[str, int]]] = field(
        default_factory=list
    )

    async def draft_conventions(
        self,
        *,
        agents_md: str | None,
        files: tuple[RepositoryFile, ...],
        languages: dict[str, int],
        changed_files: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append((agents_md, files, languages))
        return self.result


@dataclass
class FakeStore:
    cached: CachedConventions | None = None
    saved: list[CachedConventions] = field(default_factory=list)
    traces: list[tuple[UUID, tuple[ConventionsFile, ...]]] = field(default_factory=list)
    requested_keys: list[tuple[UUID, str | None, UUID]] = field(default_factory=list)
    fail_on_save: bool = False

    async def get(
        self, repository_id: UUID, agents_md_sha: str | None, prompt_version_id: UUID
    ) -> CachedConventions | None:
        self.requested_keys.append((repository_id, agents_md_sha, prompt_version_id))
        if self.cached is not None and self.cached.prompt_version_id == prompt_version_id:
            return self.cached
        return None

    async def save_and_record_trace(
        self,
        run_id: UUID,
        conventions: CachedConventions,
        trace_files: tuple[ConventionsFile, ...],
    ) -> CachedConventions:
        if self.fail_on_save:
            raise RuntimeError("trace write failed")
        saved = self.cached
        if saved is None or saved.prompt_version_id != conventions.prompt_version_id:
            self.saved.append(conventions)
            self.cached = conventions
            saved = conventions
        self.traces.append((run_id, trace_files))
        return saved


@dataclass
class FakeUnitOfWork:
    conventions: FakeStore
    commits: int = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


@dataclass
class FakeUnitOfWorkFactory:
    store: FakeStore
    units: list[FakeUnitOfWork] = field(default_factory=list)

    def __call__(self) -> FakeUnitOfWork:
        unit = FakeUnitOfWork(self.store)
        self.units.append(unit)
        return unit


def test_cache_hit_uses_exact_revision_key_without_a_model_call() -> None:
    cached = CachedConventions(
        repository_id=REPOSITORY_ID,
        agents_md_sha=AGENTS_SHA,
        prompt_version_id=PROMPT_VERSION_ID,
        key_patterns=("One.", "Two.", "Three."),
        recommendations=("One.", "Two.", "Three.", "Four.", "Five."),
        languages={"Python": 100},
    )
    source = FakeSource()
    model = FakeModel(draft())
    store = FakeStore(cached=cached)
    factory = FakeUnitOfWorkFactory(store)

    result = asyncio.run(
        GenerateRepoConventions(source, model, factory).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("current/change.py",),
        )
    )

    assert result.cache_hit is True
    assert result.conventions == cached
    assert source.calls == ["agents"]
    assert model.calls == []
    assert store.saved == []
    assert store.requested_keys == [(REPOSITORY_ID, AGENTS_SHA, PROMPT_VERSION_ID)]
    assert store.traces == [
        (
            RUN_ID,
            (
                ConventionsFile(
                    path="current/change.py",
                    relevance="Changed in this pull request; apply cached repository conventions.",
                ),
            ),
        )
    ]
    assert [unit.commits for unit in factory.units] == [0, 1]


def test_active_conventions_v2_regenerates_cache_without_using_the_system_prompt_id() -> None:
    """A conventions prompt activation is part of the cache identity on its own."""
    system_prompt_id = UUID("00000000-0000-0000-0000-000000000599")
    conventions_v1 = ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1")
    conventions_v2 = ActiveConventionsPrompt(
        UUID("00000000-0000-0000-0000-000000000598"), "conventions v2"
    )
    source = FakeSource()
    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)
    generator = GenerateRepoConventions(source, model, factory)

    first = asyncio.run(
        generator.execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=conventions_v1,
            run_id=RUN_ID,
            changed_files=("app/main.py",),
        )
    )
    second = asyncio.run(
        generator.execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=conventions_v2,
            run_id=SECOND_RUN_ID,
            changed_files=("app/main.py",),
        )
    )

    assert system_prompt_id != conventions_v1.id
    assert first.cache_hit is False
    assert second.cache_hit is False
    assert [saved.prompt_version_id for saved in store.saved] == [
        conventions_v1.id,
        conventions_v2.id,
    ]
    assert store.requested_keys == [
        (REPOSITORY_ID, AGENTS_SHA, conventions_v1.id),
        (REPOSITORY_ID, AGENTS_SHA, conventions_v2.id),
    ]
    assert len(model.calls) == 2


def test_cache_miss_fetches_bounded_context_derives_languages_and_records_draft_files() -> None:
    source = FakeSource()
    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    result = asyncio.run(
        GenerateRepoConventions(source, model, factory).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("app/main.py",),
        )
    )

    assert result.cache_hit is False
    assert result.conventions.languages == {"Python": 60, "TypeScript": 40}
    assert result.conventions.key_patterns == ("Pattern one.", "Pattern two.", "Pattern three.")
    assert source.calls == ["agents", "tree", "files:app/main.py,web/app.ts"]
    assert model.calls[0][2] == {"Python": 60, "TypeScript": 40}
    assert [file.path for file in store.traces[0][1]] == ["app/main.py"]
    assert len(store.saved) == 1
    assert [unit.commits for unit in factory.units] == [0, 1]


@pytest.mark.parametrize(
    "payload",
    [
        {"files": [], "key_patterns": ["a", "b"], "recommendations": ["a"] * 5},
        {**draft(), "unknown": True},
        {**draft(), "files": [{"path": "app/main.py", "relevance": "ok", "extra": True}]},
    ],
)
def test_draft_rejects_malformed_model_output_before_save_or_trace(
    payload: dict[str, object],
) -> None:
    source = FakeSource()
    model = FakeModel(payload)
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    with pytest.raises(ValidationError):
        asyncio.run(
            GenerateRepoConventions(source, model, factory).execute(
                repository_id=REPOSITORY_ID,
                conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
                run_id=RUN_ID,
                changed_files=("app/main.py",),
            )
        )

    assert store.saved == []
    assert store.traces == []
    assert [unit.commits for unit in factory.units] == [0]


def test_language_percentages_are_deterministic_and_ignore_unknown_extensions() -> None:
    languages = derive_languages(
        (
            RepositoryFile("ui/a.ts", 1),
            RepositoryFile("api/b.py", 1),
            RepositoryFile("README.md", 100),
            RepositoryFile("ui/c.tsx", 1),
        )
    )

    assert languages == {"Python": 34, "TSX": 33, "TypeScript": 33}


def test_draft_is_a_closed_strict_model() -> None:
    parsed = ConventionsDraft.model_validate(draft())

    assert parsed.files[0].path == "app/main.py"


def test_failed_atomic_cache_and_trace_write_never_commits() -> None:
    source = FakeSource()
    model = FakeModel(draft())
    factory = FakeUnitOfWorkFactory(FakeStore(fail_on_save=True))

    with pytest.raises(RuntimeError, match="trace write failed"):
        asyncio.run(
            GenerateRepoConventions(source, model, factory).execute(
                repository_id=REPOSITORY_ID,
                conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
                run_id=RUN_ID,
                changed_files=("app/main.py",),
            )
        )

    assert [unit.commits for unit in factory.units] == [0, 0]


def test_draft_rejects_files_from_another_pull_request_before_save_or_trace() -> None:
    source = FakeSource()
    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    with pytest.raises(ValueError, match="current pull request paths"):
        asyncio.run(
            GenerateRepoConventions(source, model, factory).execute(
                repository_id=REPOSITORY_ID,
                conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
                run_id=RUN_ID,
                changed_files=("other_pr.py",),
            )
        )

    assert store.saved == []
    assert store.traces == []


def test_cache_hit_records_the_current_pr_files_not_the_first_pr_draft() -> None:
    source = FakeSource()
    first_draft = draft()
    first_draft["files"] = [{"path": "first_pr.py", "relevance": "First pull request."}]
    model = FakeModel(first_draft)
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)
    generator = GenerateRepoConventions(source, model, factory)
    first_run = RUN_ID
    second_run = UUID("00000000-0000-0000-0000-000000000504")

    asyncio.run(
        generator.execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=first_run,
            changed_files=("first_pr.py",),
        )
    )
    second = asyncio.run(
        generator.execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=second_run,
            changed_files=("second_pr.py",),
        )
    )

    assert second.cache_hit is True
    assert [file.path for file in store.traces[0][1]] == ["first_pr.py"]
    assert [file.path for file in store.traces[1][1]] == ["second_pr.py"]
    assert store.traces[1][1][0].relevance == (
        "Changed in this pull request; apply cached repository conventions."
    )


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
                    rules=[],
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
                    ),
                )
                session.add_all(
                    (
                        workspace,
                        installation,
                        repository,
                        system_prompt,
                        conventions_v1,
                        conventions_v2,
                        rule_version,
                        code_change,
                        *runs,
                    )
                )
                await session.commit()

            conventions_input = await SqlAlchemyRunRepository(
                session_factory
            ).get_run_conventions_input(RUN_ID)
            assert conventions_input is not None
            assert conventions_input.conventions_prompt == ActiveConventionsPrompt(
                conventions_v2.id, "conventions v2"
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
