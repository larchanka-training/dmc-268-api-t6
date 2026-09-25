"""Behavioural contracts for cached repository conventions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.modules.repositories.infrastructure.models import RepoConventionDraft, RepoConventions
from app.modules.reviews.application.conventions import (
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
from app.modules.reviews.infrastructure.models import RunAction

REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000501")
PROMPT_VERSION_ID = UUID("00000000-0000-0000-0000-000000000502")
RUN_ID = UUID("00000000-0000-0000-0000-000000000503")
AGENTS_SHA = "a" * 40


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
    ) -> dict[str, object]:
        self.calls.append((agents_md, files, languages))
        return self.result


@dataclass
class FakeStore:
    cached: CachedConventions | None = None
    saved: list[CachedConventions] = field(default_factory=list)
    traces: list[tuple[UUID, tuple[ConventionsFile, ...]]] = field(default_factory=list)
    fail_on_save: bool = False

    async def get(
        self, repository_id: UUID, agents_md_sha: str | None, prompt_version_id: UUID
    ) -> CachedConventions | None:
        assert (repository_id, agents_md_sha, prompt_version_id) == (
            REPOSITORY_ID,
            AGENTS_SHA,
            PROMPT_VERSION_ID,
        )
        return self.cached

    async def save_and_record_trace(
        self, run_id: UUID, conventions: CachedConventions
    ) -> CachedConventions:
        if self.fail_on_save:
            raise RuntimeError("trace write failed")
        saved = self.cached
        if saved is None:
            self.saved.append(conventions)
            self.cached = conventions
            saved = conventions
        self.traces.append((run_id, saved.draft_files))
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
        draft_files=(ConventionsFile(path="app/main.py", relevance="Entry point."),),
    )
    source = FakeSource()
    model = FakeModel(draft())
    store = FakeStore(cached=cached)
    factory = FakeUnitOfWorkFactory(store)

    result = asyncio.run(
        GenerateRepoConventions(source, model, factory).execute(
            repository_id=REPOSITORY_ID,
            prompt_version_id=PROMPT_VERSION_ID,
            run_id=RUN_ID,
        )
    )

    assert result.cache_hit is True
    assert result.conventions == cached
    assert source.calls == ["agents"]
    assert model.calls == []
    assert store.saved == []
    assert store.traces == [(RUN_ID, cached.draft_files)]
    assert [unit.commits for unit in factory.units] == [0, 1]


def test_cache_miss_fetches_bounded_context_derives_languages_and_records_draft_files() -> None:
    source = FakeSource()
    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    result = asyncio.run(
        GenerateRepoConventions(source, model, factory).execute(
            repository_id=REPOSITORY_ID,
            prompt_version_id=PROMPT_VERSION_ID,
            run_id=RUN_ID,
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
                prompt_version_id=PROMPT_VERSION_ID,
                run_id=RUN_ID,
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
                prompt_version_id=PROMPT_VERSION_ID,
                run_id=RUN_ID,
            )
        )

    assert [unit.commits for unit in factory.units] == [0, 0]


class EmptyResult:
    def one_or_none(self) -> None:
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


def test_sqlalchemy_store_flushes_cache_draft_and_trace_without_committing() -> None:
    session = FakeSqlAlchemySession()
    conventions = CachedConventions(
        repository_id=REPOSITORY_ID,
        agents_md_sha=AGENTS_SHA,
        prompt_version_id=PROMPT_VERSION_ID,
        key_patterns=("One.", "Two.", "Three."),
        recommendations=("One.", "Two.", "Three.", "Four.", "Five."),
        languages={"Python": 100},
        draft_files=(ConventionsFile(path="app/main.py", relevance="Entry point."),),
    )

    result = asyncio.run(
        SqlAlchemyRepositoryConventionsStore(session).save_and_record_trace(RUN_ID, conventions)  # type: ignore[arg-type]
    )

    assert result == conventions
    assert session.flushes == 2
    assert [type(row) for row in session.rows] == [RepoConventions, RepoConventionDraft, RunAction]
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
        draft_files=(ConventionsFile(path="app/main.py", relevance="Entry point."),),
    )

    async def exercise() -> None:
        async with SqlAlchemyRepositoryConventionsUnitOfWork(lambda: session) as uow:  # type: ignore[arg-type]
            await uow.conventions.save_and_record_trace(RUN_ID, conventions)
            await uow.commit()

    with pytest.raises(RuntimeError, match="late trace flush failed"):
        asyncio.run(exercise())

    assert session.rollbacks == 1
    assert session.rows == []
    assert session.closed is True
