import asyncio
from typing import cast
from uuid import UUID

import pytest
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.worker as worker
from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.process_run import RunDiffProvider
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest


def test_process_review_run_composes_repository_and_processor_from_a_shared_factory(
    monkeypatch: MonkeyPatch,
) -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    factory = cast(async_sessionmaker[AsyncSession], object())

    class Vcs:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            raise AssertionError("composition only")

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            raise AssertionError("composition only")

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            raise AssertionError("composition only")

    vcs = Vcs()

    class Provider(RunDiffProvider):
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            raise AssertionError("the composition test does not call the provider")

        async def fetch_file_content(
            self, *, code_change_id: UUID, head_sha: str, path: str
        ) -> str:
            raise AssertionError("the composition test does not call the provider")

        async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
            raise AssertionError("the composition test does not call the provider")

        async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
            raise AssertionError("the composition test does not call the provider")

        async def fetch_files(
            self, repository_id: UUID, paths: tuple[str, ...]
        ) -> tuple[RepositoryFile, ...]:
            raise AssertionError("the composition test does not call the provider")

        async def draft_conventions(
            self,
            *,
            request: ConventionsRequest,
        ) -> dict[str, object]:
            raise AssertionError("the composition test does not call the provider")

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
            raise AssertionError("the composition test does not call the provider")

        async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
            raise AssertionError("the composition test does not call the provider")

        async def publish_review(
            self,
            *,
            commit_sha: str,
            body: str,
            findings: tuple[PublishedFinding, ...],
            idempotency_key: str,
        ) -> None:
            raise AssertionError("the composition test does not call the provider")

    class Repository:
        def __init__(self, received_factory: async_sessionmaker[AsyncSession]) -> None:
            assert received_factory is factory

    class Processor:
        def __init__(
            self,
            repository: Repository,
            provider: Provider,
            cache: object,
            conventions: object,
            *,
            vcs_provider: Vcs,
            trace: object = None,
        ) -> None:
            assert trace is None
            assert isinstance(repository, Repository)
            assert isinstance(provider, Provider)
            assert cache is not None
            assert conventions is not None
            assert vcs_provider is vcs

    class Pipeline:
        def __init__(
            self, processor: Processor, repository: object, model: Provider, publisher: object
        ) -> None:
            assert isinstance(processor, Processor)
            assert repository is not None
            assert isinstance(model, Provider)
            assert publisher is not None

        async def execute(self, received_run_id: UUID) -> bool:
            assert received_run_id == run_id
            return True

    monkeypatch.setattr(worker, "SqlAlchemyRunRepository", Repository)
    monkeypatch.setattr(worker, "ReviewRunProcessor", Processor)
    monkeypatch.setattr(worker, "ExecuteReviewRun", Pipeline)

    assert asyncio.run(worker.process_review_run(run_id, Provider(), factory, vcs)) is True


def test_review_worker_disposes_its_single_engine_at_shutdown(monkeypatch: MonkeyPatch) -> None:
    class Provider(RunDiffProvider):
        async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
            raise AssertionError("the lifecycle test does not call the provider")

        async def fetch_file_content(
            self, *, code_change_id: UUID, head_sha: str, path: str
        ) -> str:
            raise AssertionError("the lifecycle test does not call the provider")

        async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
            raise AssertionError("the lifecycle test does not call the provider")

        async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
            raise AssertionError("the lifecycle test does not call the provider")

        async def fetch_files(
            self, repository_id: UUID, paths: tuple[str, ...]
        ) -> tuple[RepositoryFile, ...]:
            raise AssertionError("the lifecycle test does not call the provider")

        async def draft_conventions(
            self,
            *,
            request: ConventionsRequest,
        ) -> dict[str, object]:
            raise AssertionError("the lifecycle test does not call the provider")

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
            raise AssertionError("the lifecycle test does not call the provider")

        async def draft_review(self, *, context: ReviewContext) -> dict[str, object]:
            raise AssertionError("the lifecycle test does not call the provider")

        async def publish_review(
            self,
            *,
            commit_sha: str,
            body: str,
            findings: tuple[PublishedFinding, ...],
            idempotency_key: str,
        ) -> None:
            raise AssertionError("the lifecycle test does not call the provider")

    class Engine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()

    class Vcs:
        async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
            raise AssertionError("lifecycle only")

        async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
            raise AssertionError("lifecycle only")

        async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
            raise AssertionError("lifecycle only")

    received: list[tuple[str, bool]] = []

    def create_engine(database_url: str, *, pool_pre_ping: bool) -> AsyncEngine:
        received.append((database_url, pool_pre_ping))
        return cast(AsyncEngine, engine)

    monkeypatch.setattr(worker, "create_async_engine", create_engine)

    async def use_worker() -> None:
        async with worker.review_worker(
            Provider(), database_url="postgresql+psycopg://test", vcs_provider=Vcs()
        ):
            assert engine.disposed is False

    asyncio.run(use_worker())

    assert received == [("postgresql+psycopg://test", True)]
    assert engine.disposed is True


def test_review_worker_starts_without_app_secrets_but_fails_run_closed(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)

    class Engine:
        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(
        worker,
        "create_async_engine",
        lambda database_url, *, pool_pre_ping: cast(AsyncEngine, Engine()),
    )

    async def exercise() -> None:
        async with worker.review_worker(
            cast(worker.ReviewWorkerProvider, object()),
            database_url="postgresql+psycopg://test",
        ) as instance:
            with pytest.raises(RuntimeError, match="GitHub VCS provider"):
                await instance.process_review_run(UUID("00000000-0000-0000-0000-000000000100"))

    asyncio.run(exercise())
