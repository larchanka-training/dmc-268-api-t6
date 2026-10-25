"""Behavioural contracts for cached repository conventions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace, TracebackType
from typing import Any
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
    ConventionsRequest,
    GenerateRepoConventions,
    PolicyFilteredConventionsSource,
    RepositoryFile,
    RepositorySnapshot,
    derive_languages,
)
from app.modules.reviews.application.conventions_prompt import (
    build_conventions_prompt,
    fit_conventions_request,
)
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_budget import EstimatedTokenCounter
from app.modules.reviews.application.prompt_builder import ReviewRule
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsStore,
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.llm.gateway import HeuristicTokenCounter
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
            "Recommendation one (from: standard/correctness)",
            "Recommendation two (from: standard/correctness)",
            "Recommendation three (from: standard/correctness)",
            "Recommendation four (from: standard/correctness)",
            "Recommendation five (from: standard/correctness)",
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
    calls: list[ConventionsRequest] = field(default_factory=list)

    async def draft_conventions(
        self,
        *,
        request: ConventionsRequest,
    ) -> dict[str, object]:
        self.calls.append(request)
        return self.result


@dataclass
class FakeStore:
    cached: CachedConventions | None = None
    saved: list[CachedConventions] = field(default_factory=list)
    traces: list[tuple[UUID, tuple[ConventionsFile, ...]]] = field(default_factory=list)
    requested_keys: list[tuple[UUID, str | None, UUID]] = field(default_factory=list)
    requested_policy_keys: list[str | None] = field(default_factory=list)
    fail_on_save: bool = False

    async def get(
        self,
        repository_id: UUID,
        agents_md_sha: str | None,
        prompt_version_id: UUID,
        policy_key: str | None = None,
    ) -> CachedConventions | None:
        self.requested_keys.append((repository_id, agents_md_sha, prompt_version_id))
        self.requested_policy_keys.append(policy_key)
        if (
            self.cached is not None
            and self.cached.prompt_version_id == prompt_version_id
            and self.cached.policy_key == policy_key
        ):
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
    assert model.calls[0].languages == {"Python": 60, "TypeScript": 40}
    assert [file.path for file in store.traces[0][1]] == ["app/main.py"]
    assert len(store.saved) == 1
    assert [unit.commits for unit in factory.units] == [0, 1]


def test_cache_miss_passes_the_complete_versioned_conventions_request() -> None:
    source = FakeSource()
    model = FakeModel(
        {
            **draft(),
            "files": [
                {"path": "app/main.py", "relevance": "Application entrypoint."},
                {"path": "web/app.ts", "relevance": "Web application."},
            ],
        }
    )
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)
    prompt = ActiveConventionsPrompt(PROMPT_VERSION_ID, "stored conventions prompt")
    rules = (
        ReviewRule(
            name="Database boundary",
            include=("app/**/*.py",),
            exclude=("tests/**",),
            checks=("Use cases commit once.",),
        ),
    )

    asyncio.run(
        GenerateRepoConventions(source, model, factory).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=prompt,
            rules=rules,
            run_id=RUN_ID,
            changed_files=("app/main.py", "web/app.ts"),
        )
    )

    assert model.calls == [
        ConventionsRequest(
            system="stored conventions prompt",
            rules=rules,
            agents_md="Always use uv.",
            repo_tree=("app/main.py", "web/app.ts", "README.md"),
            repo_files=(
                RepositoryFile("app/main.py", 10, content="content for app/main.py"),
                RepositoryFile("web/app.ts", 10, content="content for web/app.ts"),
            ),
            languages={"Python": 60, "TypeScript": 40},
            changed_files=("app/main.py", "web/app.ts"),
        )
    ]


def test_conventions_request_limits_repository_files_to_ten() -> None:
    """The model never receives more than ten repository file payloads."""

    @dataclass
    class ManyFilesSource(FakeSource):
        async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
            assert repository_id == REPOSITORY_ID
            self.calls.append("tree")
            return tuple(RepositoryFile(f"app/file-{index:02}.py", 10) for index in range(11))

    source = ManyFilesSource()
    model = FakeModel(
        {
            **draft(),
            "files": [{"path": "app/file-00.py", "relevance": "Application module."}],
        }
    )
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    asyncio.run(
        GenerateRepoConventions(source, model, factory).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("app/file-00.py",),
        )
    )

    assert source.calls == [
        "agents",
        "tree",
        "files:app/file-00.py,app/file-01.py,app/file-02.py,app/file-03.py,app/file-04.py,"
        "app/file-05.py,app/file-06.py,app/file-07.py,app/file-08.py,app/file-09.py",
    ]
    assert [file.path for file in model.calls[0].repo_files] == [
        f"app/file-{index:02}.py" for index in range(10)
    ]
    assert (
        "[1 more repository file contexts omitted]" in build_conventions_prompt(model.calls[0]).user
    )


def test_conventions_request_limits_each_repository_file_to_three_hundred_lines() -> None:
    """The model receives only the first 300 lines of each fetched payload."""

    @dataclass
    class LongFileSource(FakeSource):
        async def fetch_files(
            self, repository_id: UUID, paths: tuple[str, ...]
        ) -> tuple[RepositoryFile, ...]:
            assert repository_id == REPOSITORY_ID
            self.calls.append(f"files:{','.join(paths)}")
            content = "".join(f"line {index}\n" for index in range(301))
            return (RepositoryFile("app/main.py", len(content), content=content),)

    source = LongFileSource()
    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    asyncio.run(
        GenerateRepoConventions(source, model, factory).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("app/main.py",),
        )
    )

    content = model.calls[0].repo_files[0].content
    assert content is not None
    assert content.splitlines() == [f"line {index}" for index in range(300)]
    assert model.calls[0].repo_files[0].omitted_lines == 1
    assert '<line n="300">line 299</line>\n[1 more lines omitted]\n</file>' in (
        build_conventions_prompt(model.calls[0]).user
    )


def test_conventions_renderer_marks_all_known_omissions_and_escapes_content() -> None:
    request = ConventionsRequest(
        system="P",
        rules=(),
        agents_md=None,
        repo_tree=("a&b.py", "extra<.py"),
        repo_files=(RepositoryFile("a&b.py", 10, "x < y & z", omitted_lines=2),),
        languages={},
        changed_files=("a&b.py", *(f"f{index:03}.py" for index in range(100))),
        omitted_tree_paths=2,
    )

    user = build_conventions_prompt(request).user

    assert "<repo_tree>\na&amp;b.py\nextra&lt;.py\n[2 more paths omitted]\n</repo_tree>" in user
    assert (
        '<file path="a&amp;b.py">\n<line n="1">x &lt; y &amp; z</line>\n'
        "[2 more lines omitted]\n</file>"
    ) in user
    assert "[3 more repository file contexts omitted]" in user
    assert "f098.py\n[1 more changed paths omitted]\n</changed_files>" in user


def test_conventions_renderer_has_no_omission_marker_for_empty_or_complete_context() -> None:
    empty = ConventionsRequest("P", (), None, (), (), {}, ())
    complete = ConventionsRequest(
        "P",
        (),
        None,
        ("app/main.py",),
        (RepositoryFile("app/main.py", 1, "x"),),
        {},
        ("app/main.py",),
    )

    assert " more " not in build_conventions_prompt(empty).user
    assert " more " not in build_conventions_prompt(complete).user


def test_fitting_adds_prior_tree_omissions_to_the_exact_count() -> None:
    class CharCounter:
        def count(self, text: str) -> int:
            return len(text)

    request = ConventionsRequest("P", (), None, ("a.py", "b.py"), (), {}, (), 2)

    fitted = fit_conventions_request(request, max_prompt_tokens=0, counter=CharCounter())

    assert fitted.repo_tree == ()
    assert fitted.omitted_tree_paths == 4
    assert "[4 more paths omitted]" in build_conventions_prompt(fitted).user


def test_missing_selected_file_content_is_counted_as_omitted_context() -> None:
    @dataclass
    class MissingContentSource(FakeSource):
        async def fetch_files(
            self, repository_id: UUID, paths: tuple[str, ...]
        ) -> tuple[RepositoryFile, ...]:
            assert paths == ("app/main.py", "web/app.ts")
            return (RepositoryFile("app/main.py", 10, "content"),)

    model = FakeModel(draft())
    asyncio.run(
        GenerateRepoConventions(
            MissingContentSource(), model, FakeUnitOfWorkFactory(FakeStore())
        ).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("app/main.py",),
        )
    )

    assert (
        "[2 more repository file contexts omitted]" in build_conventions_prompt(model.calls[0]).user
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"files": [], "key_patterns": ["a", "b"], "recommendations": ["a"] * 5},
        {**draft(), "unknown": True},
        {**draft(), "files": [{"path": "app/main.py", "relevance": "ok", "extra": True}]},
        {**draft(), "key_patterns": ["a" * 161, "Two.", "Three."]},
        {
            **draft(),
            "recommendations": [
                "Missing a provenance suffix.",
                "Recommendation two (from: standard/correctness)",
                "Recommendation three (from: standard/correctness)",
                "Recommendation four (from: standard/correctness)",
                "Recommendation five (from: standard/correctness)",
            ],
        },
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


def test_draft_rejects_empty_files_for_a_changed_pull_request_before_save_or_trace() -> None:
    source = FakeSource()
    payload = draft()
    payload["files"] = []
    model = FakeModel(payload)
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    with pytest.raises(ValueError, match="at least one file"):
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


def test_a_pull_request_over_the_traced_limit_keeps_every_path_in_the_trace() -> None:
    changed = tuple(f"src/module_{index:03}.py" for index in range(101))
    answer = dict(
        draft(),
        files=[{"path": path, "relevance": "changed module"} for path in changed[:100]],
    )
    model = FakeModel(answer)
    store = FakeStore()

    asyncio.run(
        GenerateRepoConventions(FakeSource(), model, FakeUnitOfWorkFactory(store)).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=changed,
        )
    )

    assert model.calls[0].traced_files == changed[:100]
    traced = store.traces[0][1]
    assert [file.path for file in traced] == list(changed)
    assert traced[-1].relevance.startswith("Changed in this pull request")


def test_cache_miss_shows_the_conventions_model_nothing_the_policy_withholds() -> None:
    source = FakeSource()
    model = FakeModel(draft())
    store = FakeStore()
    filtered = PolicyFilteredConventionsSource(source, lambda path: path.startswith("web/"))

    result = asyncio.run(
        GenerateRepoConventions(filtered, model, FakeUnitOfWorkFactory(store)).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("app/main.py",),
            policy_key="1" * 64,
        )
    )

    # The actual conventions request: neither the name nor the content of web/app.ts.
    request = model.calls[0]
    assert request.repo_tree == ("app/main.py", "README.md")
    assert [file.path for file in request.repo_files] == ["app/main.py"]
    assert "web/app.ts" not in repr(request)
    # The ignored file is not even loaded.
    assert source.calls == ["agents", "tree", "files:app/main.py"]
    # Language shares are computed over what is left after the policy.
    assert request.languages == {"Python": 100}
    assert result.conventions.policy_key == "1" * 64
    assert store.requested_policy_keys == ["1" * 64]


def test_conventions_cached_under_another_ignore_policy_are_not_reused() -> None:
    cached = CachedConventions(
        repository_id=REPOSITORY_ID,
        agents_md_sha=AGENTS_SHA,
        prompt_version_id=PROMPT_VERSION_ID,
        key_patterns=("One.", "Two.", "Three."),
        recommendations=("One.", "Two.", "Three.", "Four.", "Five."),
        languages={"Python": 100},
        policy_key="1" * 64,
    )

    def generate(policy_key: str | None) -> tuple[bool, FakeModel]:
        model = FakeModel(draft())
        result = asyncio.run(
            GenerateRepoConventions(
                FakeSource(), model, FakeUnitOfWorkFactory(FakeStore(cached=cached))
            ).execute(
                repository_id=REPOSITORY_ID,
                conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
                run_id=RUN_ID,
                changed_files=("app/main.py",),
                policy_key=policy_key,
            )
        )
        return result.cache_hit, model

    assert generate("1" * 64)[0] is True
    changed_policy_hit, model = generate("2" * 64)
    assert changed_policy_hit is False
    assert len(model.calls) == 1
    assert generate(None)[0] is False


def test_filtered_source_drops_a_withheld_file_even_if_the_inner_source_returns_it() -> None:
    class Leaky(FakeSource):
        async def fetch_files(
            self, repository_id: UUID, paths: tuple[str, ...]
        ) -> tuple[RepositoryFile, ...]:
            return (
                RepositoryFile("app/main.py", 10, content="ok"),
                RepositoryFile("web/app.ts", 10, content="leak"),
            )

    filtered = PolicyFilteredConventionsSource(Leaky(), lambda path: path.startswith("web/"))

    files = asyncio.run(filtered.fetch_files(REPOSITORY_ID, ("app/main.py", "web/app.ts")))

    assert [file.path for file in files] == ["app/main.py"]


def test_conventions_provenance_guard_rejects_directory_traversal() -> None:
    from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode

    class TraversalSource(FakeSource):
        async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
            return (RepositoryFile("../secret.py", 50),)

        async def fetch_files(
            self, repository_id: UUID, paths: tuple[str, ...]
        ) -> tuple[RepositoryFile, ...]:
            return (RepositoryFile("../secret.py", 10, content="secret"),)

    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(
            GenerateRepoConventions(TraversalSource(), model, factory).execute(
                repository_id=REPOSITORY_ID,
                conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
                run_id=RUN_ID,
                changed_files=("app/main.py",),
            )
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert "directory traversal" in exc_info.value.message
    # Pre-send guard stops call BEFORE model is invoked!
    assert len(model.calls) == 0


def test_conventions_provenance_guard_rejects_disallowed_file_in_tree_only() -> None:
    from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode

    class TreeDisallowedSource(FakeSource):
        async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
            return (
                RepositoryFile("app/main.py", 60),
                RepositoryFile("../secret.py", 50),
            )

        async def fetch_files(
            self, repository_id: UUID, paths: tuple[str, ...]
        ) -> tuple[RepositoryFile, ...]:
            return (RepositoryFile("app/main.py", 10, content="ok"),)

    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    with pytest.raises(PolicyError) as exc_info:
        asyncio.run(
            GenerateRepoConventions(TreeDisallowedSource(), model, factory).execute(
                repository_id=REPOSITORY_ID,
                conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
                run_id=RUN_ID,
                changed_files=("app/main.py",),
            )
        )
    assert exc_info.value.code == PolicyErrorCode.CONTEXT_VIOLATION
    assert len(model.calls) == 0


def test_conventions_execution_with_passed_snapshot_and_agents_md() -> None:
    from dataclasses import replace

    from app.modules.reviews.application.llm import RunCallContext
    from app.modules.reviews.application.policy_snapshot import (
        CallManifest,
        ExecutionProfile,
        PolicySnapshot,
        PromptBindings,
        make_policy_document_missing,
        make_policy_document_present,
    )
    from app.modules.reviews.infrastructure.llm.models import GatewayConventionsModel

    agents_content = "Always use uv.\r\nSecond line.\r\n"

    class CrlfSource(FakeSource):
        async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
            assert repository_id == REPOSITORY_ID
            self.calls.append("agents")
            return RepositorySnapshot(content=agents_content, sha=AGENTS_SHA)

    snapshot = PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid4(),
        run_id=RUN_ID,
        repository_id=REPOSITORY_ID,
        policy_base_sha="base_commit_sha_distinct_from_agents_blob",
        rules=make_policy_document_missing(".review/rules.md"),
        agents=make_policy_document_present("AGENTS.md", agents_content),
        ignore_patterns=(),
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid4(),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=PromptBindings("cs1", "cs2", "cs3"),
        execution_profile=ExecutionProfile.fast("m1", "m2"),
        policy_digest="digest_base",
    )

    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    result = asyncio.run(
        GenerateRepoConventions(CrlfSource(), model, factory).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("app/main.py",),
            snapshot=snapshot,
        )
    )
    # R1 (#130): the snapshot's normalized AGENTS.md; the provider is not read again.
    assert result.agents_md == snapshot.agents.text
    assert len(model.calls) == 1
    assert model.calls[0].snapshot is snapshot

    # Verify GatewayConventionsModel builds manifest AFTER fit_conventions_request
    captured_manifests: list[CallManifest | None] = []

    class FakeGateway:
        def max_prompt_tokens(self, engine: object = None) -> int:
            return 300

        def token_counter(self) -> HeuristicTokenCounter:
            return HeuristicTokenCounter(3.0)

        async def generate(self, task: object, run: object) -> object:
            captured_manifests.append(getattr(task, "base_manifest", None))

            class Resp:
                output = draft()

            return Resp()

    from app.modules.reviews.application.prompt_budget import token_counter_for
    from app.modules.reviews.infrastructure.llm.gateway import HeuristicTokenCounter

    conv_req = model.calls[0]
    large_tree = tuple(f"app/sub_{i}/file_{i}.py" for i in range(100))
    conv_req_large = replace(
        conv_req,
        repo_tree=large_tree,
    )
    run_ctx = RunCallContext(
        run_id=RUN_ID,
        workspace_id=uuid4(),
        attempt=1,
        engine="fast",
        deadline=datetime(2026, 10, 23, 18, 0, tzinfo=UTC),
    )
    gw_model = GatewayConventionsModel(FakeGateway(), run_ctx)  # type: ignore[arg-type]
    asyncio.run(gw_model.draft_conventions(request=conv_req_large))
    assert len(captured_manifests) == 1
    manifest = captured_manifests[0]
    assert manifest is not None
    counter = token_counter_for(snapshot.execution_profile.counter_version)
    fitted = fit_conventions_request(conv_req_large, max_prompt_tokens=300, counter=counter)
    assert len(fitted.repo_tree) < len(large_tree)
    tree_blocks = [b for b in manifest.blocks if b.block_id.startswith("tree-")]
    assert len(tree_blocks) == len(fitted.repo_tree)
    assert len(tree_blocks) < len(large_tree)


def test_conventions_execution_with_bom_and_lone_cr_in_agents_md() -> None:
    from app.modules.reviews.application.policy_documents import normalize_document
    from app.modules.reviews.application.policy_snapshot import (
        ExecutionProfile,
        PolicySnapshot,
        PromptBindings,
        make_policy_document_missing,
        make_policy_document_present,
    )

    raw_agents_bytes = b"\xef\xbb\xbfAlways use uv.\rLone CR line.\r\nCRLF line.\n"
    normalized_agents = normalize_document("AGENTS.md", raw_agents_bytes, blob_oid="blob_oid_123")

    class RawAgentsSource(FakeSource):
        async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
            # Source returns string containing BOM and lone CR
            content = raw_agents_bytes.decode("utf-8")
            return RepositorySnapshot(content=content, sha=AGENTS_SHA)

    snapshot = PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid4(),
        run_id=RUN_ID,
        repository_id=REPOSITORY_ID,
        policy_base_sha="base_commit_sha",
        rules=make_policy_document_missing(".review/rules.md"),
        agents=make_policy_document_present("AGENTS.md", normalized_agents.text or ""),
        ignore_patterns=(),
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid4(),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=PromptBindings("cs1", "cs2", "cs3"),
        execution_profile=ExecutionProfile.fast("m1", "m2"),
        policy_digest="digest_base",
    )

    model = FakeModel(draft())
    store = FakeStore()
    factory = FakeUnitOfWorkFactory(store)

    _ = asyncio.run(
        GenerateRepoConventions(RawAgentsSource(), model, factory).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v1"),
            run_id=RUN_ID,
            changed_files=("app/main.py",),
            snapshot=snapshot,
        )
    )
    assert len(model.calls) == 1
    req = model.calls[0]
    assert req.snapshot is snapshot


def test_draft_conventions_counter_v1_matches_transport() -> None:
    import json
    from dataclasses import replace

    import httpx

    from app.modules.reviews.application.policy_snapshot import (
        ExecutionProfile,
        PolicySnapshot,
        PromptBindings,
        make_policy_document_missing,
    )
    from app.modules.reviews.infrastructure.llm.models import GatewayConventionsModel
    from tests.test_llm_gateway import Harness, completion

    snapshot = PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid4(),
        run_id=RUN_ID,
        repository_id=REPOSITORY_ID,
        policy_base_sha="base_commit_sha",
        rules=make_policy_document_missing(".review/rules.md"),
        agents=make_policy_document_missing("AGENTS.md"),
        ignore_patterns=(),
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid4(),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=PromptBindings("cs1", "cs2", "cs3"),
        execution_profile=replace(ExecutionProfile.fast("m1", "m2"), counter_version="counter/v1"),
        policy_digest="digest_base",
    )

    tree = tuple(f"app/path_{i:04d}/module_{i:04d}.py" for i in range(1, 10000))
    req_v1 = ConventionsRequest(
        system="SYS",
        rules=(),
        agents_md=None,
        repo_tree=tree,
        repo_files=(),
        languages={},
        changed_files=("app/main.py",),
        snapshot=snapshot,
    )

    harness = Harness([completion(json.dumps(draft()))])

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness._handle)) as client:
            gw = harness.gateway(client)
            model = GatewayConventionsModel(gw, harness.run)
            await model.draft_conventions(request=req_v1)

    asyncio.run(exercise())
    body = json.loads(harness.requests[0].content)
    text = body["messages"][1]["content"]
    tree_count = text.count("module_")
    # 7164 paths fitted under counter/v1 (vs 5370 under counter/v2) in the profile's 52k send
    # budget (60k input minus 8k output): the snapshot path fits on send_budget_for (#130).
    assert tree_count == 7164
    assert tree_count > 5370


# ---------- #130 F1-10: snapshot-path conventions (R1, R18) ----------


def _conventions_snapshot(agents: str | None, *, max_input_tokens: int = 60_000) -> PolicySnapshot:
    from app.modules.reviews.application.policy_snapshot import (
        ExecutionProfile,
        PromptBindings,
        make_policy_document_missing,
        make_policy_document_present,
    )

    return PolicySnapshot(
        schema_version=1,
        snapshot_id=uuid4(),
        run_id=RUN_ID,
        repository_id=REPOSITORY_ID,
        policy_base_sha="c" * 40,
        rules=make_policy_document_missing(".review/rules.md"),
        agents=(
            make_policy_document_missing("AGENTS.md")
            if agents is None
            else make_policy_document_present("AGENTS.md", agents, blob_oid="b" * 40)
        ),
        ignore_patterns=(),
        ignore_digest="id_0",
        parser_profile="rules-markdown/v1",
        parser_version="4.0.0",
        matcher_version="review-ignore/v1",
        limits_version="policy-limits/v1",
        builtin_exclusions_version="builtin-exclusions/v1",
        priority_matcher_version="priority-matcher/v1",
        service_defaults_rule_version_id=uuid4(),
        defaults_checksum="282d61c2da897152046db742fbd9bfda061ba1c71ce32759d47e042c06b0ff52",
        prompt_bindings=PromptBindings("cs1", "cs2", "cs3"),
        execution_profile=replace(
            ExecutionProfile.fast("m1", "m2"), max_input_tokens=max_input_tokens
        ),
        policy_digest="digest_base",
    )


@pytest.mark.parametrize(
    ("agents", "text", "sha"),
    [
        ("Snapshot rules.\r\nSecond line.\r\n", "Snapshot rules.\nSecond line.\n", "b" * 40),
        (None, None, None),
    ],
    ids=["present", "missing"],
)
def test_snapshot_path_conventions_take_agents_md_from_the_snapshot_without_a_fetch(
    agents: str | None, text: str | None, sha: str | None
) -> None:
    # R1: fetch_agents_md is the unchecked second read; the snapshot's document replaces it,
    # in the model input, the cache key and the result.
    source = FakeSource()
    store = FakeStore()
    model = FakeModel(draft())

    result = asyncio.run(
        GenerateRepoConventions(source, model, FakeUnitOfWorkFactory(store)).execute(
            repository_id=REPOSITORY_ID,
            conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v2"),
            run_id=RUN_ID,
            changed_files=("app/main.py",),
            snapshot=_conventions_snapshot(agents),
        )
    )

    assert "agents" not in source.calls
    assert model.calls[0].agents_md == text
    assert model.calls[0].agents_md_sha == sha
    assert store.requested_keys == [(REPOSITORY_ID, sha, PROMPT_VERSION_ID)]
    assert store.saved[0].agents_md_sha == sha
    assert result.agents_md == text


def test_snapshot_path_conventions_build_no_manifest_and_guard_before_the_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R18: the model fits, then builds the one manifest and guards it; a legacy run keeps
    # the use case's pre-fit manifest and guard.
    import app.modules.reviews.application.conventions as conventions_module
    from app.modules.reviews.application.prompt_budget import (
        verify_provenance_pre_send_guard as guard,
    )

    built: list[str] = []
    guarded: list[str] = []
    build = conventions_module.build_conventions_manifest

    def spy_build(*args: Any, **kwargs: Any) -> object:
        built.append("manifest")
        return build(*args, **kwargs)

    def spy_guard(*args: Any, **kwargs: Any) -> None:
        guarded.append("guard")
        guard(*args, **kwargs)

    monkeypatch.setattr(conventions_module, "build_conventions_manifest", spy_build)
    monkeypatch.setattr(conventions_module, "verify_provenance_pre_send_guard", spy_guard)

    def run(snapshot: PolicySnapshot | None) -> None:
        asyncio.run(
            GenerateRepoConventions(
                FakeSource(), FakeModel(draft()), FakeUnitOfWorkFactory(FakeStore())
            ).execute(
                repository_id=REPOSITORY_ID,
                conventions_prompt=ActiveConventionsPrompt(PROMPT_VERSION_ID, "conventions v2"),
                run_id=RUN_ID,
                changed_files=("app/main.py",),
                snapshot=snapshot,
            )
        )

    run(_conventions_snapshot("Use uv.\n"))
    assert (built, guarded) == ([], [])
    run(None)
    assert (built, guarded) == (["manifest"], ["guard"])


class _BudgetGateway:
    """The gateway's prompt window is wide: only the snapshot profile can narrow it."""

    def __init__(self) -> None:
        self.tasks: list[object] = []

    def max_prompt_tokens(self, engine: object = None) -> int:
        return 100_000

    def token_counter(self) -> HeuristicTokenCounter:
        return HeuristicTokenCounter(3.0)

    async def generate(self, task: object, run: object) -> object:
        self.tasks.append(task)
        return SimpleNamespace(output=draft())


def _gateway_manifest(snapshot: PolicySnapshot | None, request: ConventionsRequest) -> Any:
    from app.modules.reviews.application.llm import RunCallContext
    from app.modules.reviews.application.policy_snapshot import CallManifest
    from app.modules.reviews.infrastructure.llm.models import GatewayConventionsModel

    gateway = _BudgetGateway()
    run = RunCallContext(
        run_id=RUN_ID,
        workspace_id=uuid4(),
        attempt=1,
        engine="fast",
        deadline=datetime(2026, 10, 25, 18, 0, tzinfo=UTC),
    )
    model = GatewayConventionsModel(gateway, run)  # type: ignore[arg-type]
    asyncio.run(model.draft_conventions(request=replace(request, snapshot=snapshot)))
    (task,) = gateway.tasks
    manifest = task.base_manifest  # type: ignore[attr-defined]
    assert isinstance(manifest, CallManifest)
    return manifest


def test_snapshot_conventions_fit_on_the_profile_send_budget_and_count_every_block() -> None:
    # Plan "Conventions (R18)": the snapshot path fits on send_budget_for (the conventions call
    # takes the review route), then builds the manifest with the pinned counter.
    request = ConventionsRequest(
        system="SYS",
        rules=(),
        agents_md=None,
        repo_tree=tuple(f"app/sub_{index:03d}/module.py" for index in range(100)),
        repo_files=(),
        languages={},
        changed_files=("app/main.py",),
    )
    # 8_300 input tokens minus the default 8_000 output tokens: a 300-token send budget.
    snapshot = _conventions_snapshot(None, max_input_tokens=8_300)

    snapshot_manifest = _gateway_manifest(snapshot, request)
    legacy_manifest = _gateway_manifest(None, request)

    def tree(manifest: Any) -> int:
        return sum(block.block_id.startswith("tree-") for block in manifest.blocks)

    fitted = fit_conventions_request(
        replace(request, snapshot=snapshot), max_prompt_tokens=300, counter=EstimatedTokenCounter()
    )
    assert tree(snapshot_manifest) == len(fitted.repo_tree) == 30
    assert tree(legacy_manifest) == 100
    assert all(block.tokens is not None for block in snapshot_manifest.blocks)
    assert all(block.tokens is None for block in legacy_manifest.blocks)


def test_snapshot_conventions_manifest_counts_each_block_as_rendered() -> None:
    from app.modules.reviews.application.conventions import build_conventions_manifest

    request = ConventionsRequest(
        system="SYS",
        rules=(ReviewRule(name="Uv", include=(), exclude=(), checks=("Use uv.",)),),
        agents_md="Snapshot rules.\n",
        repo_tree=("app/main.py",),
        repo_files=(RepositoryFile("app/main.py", 6, "x = 1\n"),),
        languages={},
        changed_files=("app/main.py",),
    )
    snapshot = _conventions_snapshot("Snapshot rules.\n")

    manifest = build_conventions_manifest(
        request, snapshot=snapshot, counter=EstimatedTokenCounter()
    )

    assert [(block.block_id, block.tokens) for block in manifest.blocks] == [
        ("agents-md", 14),
        ("service-defaults", 34),
        ("file-0", 20),
        ("tree-0", 4),
        ("changed-0", 4),
    ]
