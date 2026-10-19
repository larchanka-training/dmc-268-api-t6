from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.worker as worker
from app.bootstrap.code_index_worker import CodeIndexSettings
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.process_run import RunDiffProvider
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest


def test_process_review_run_composes_repository_and_processor_from_a_shared_factory(
    monkeypatch: MonkeyPatch,
) -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    factory = cast(async_sessionmaker[AsyncSession], object())

    class Vcs:
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            raise AssertionError("composition only")

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            raise AssertionError("composition only")

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
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
        def __init__(
            self,
            received_factory: async_sessionmaker[AsyncSession],
            *,
            allow_unscoped: bool = False,
        ) -> None:
            assert received_factory is factory
            assert allow_unscoped is True

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
            uow_factory: object = None,
            context_stage: object = None,
            vcs_provider_selector: object = None,
        ) -> None:
            assert trace is None
            assert context_stage is None
            assert vcs_provider_selector is None
            assert isinstance(repository, Repository)
            assert isinstance(provider, Provider)
            assert cache is not None
            assert conventions is not None
            assert vcs_provider is vcs
            assert uow_factory is not None

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
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            await checkpoint()
            raise AssertionError("lifecycle only")

        async def get_diff(
            self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
        ) -> tuple[VcsFile, ...]:
            await checkpoint()
            raise AssertionError("lifecycle only")

        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
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


_BASE_ENV = {"DATABASE_URL": "postgresql+psycopg://test", "RABBITMQ_URL": "amqp://test"}


def test_worker_settings_read_the_code_index_variables() -> None:
    settings = worker.WorkerSettings.from_environment(
        {**_BASE_ENV, "CODE_INDEX_MAX_CONCURRENT": "3"}
    )

    assert settings.code_index == CodeIndexSettings(max_concurrent=3)
    assert worker.WorkerSettings.from_environment(_BASE_ENV).code_index == CodeIndexSettings()
    with pytest.raises(ValueError, match="CODE_INDEX_SOFT_CUTOFF_SECONDS"):
        worker.WorkerSettings.from_environment(
            {**_BASE_ENV, "CODE_INDEX_SOFT_CUTOFF_SECONDS": "900"}
        )


class _IndexStopped(Exception):
    pass


def test_run_worker_consumes_index_repo_beside_the_review_queues(
    monkeypatch: MonkeyPatch,
) -> None:
    """AC1: the index consumer runs in the worker process, apart from the review consumers."""
    consumed: list[str] = []
    composed: dict[str, Any] = {}
    channels = SimpleNamespace(connection=object(), publisher=object())

    async def consumer_queue(name: str) -> str:
        return name

    channels.consumer_queue = consumer_queue

    @asynccontextmanager
    async def amqp_channels(url: str, delays: object) -> AsyncIterator[SimpleNamespace]:
        yield channels

    async def consume(queue: str, handle: Callable[[Any], Awaitable[None]]) -> None:
        consumed.append(queue)
        await asyncio.Event().wait()

    async def leader(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    class IndexConsumer:
        async def consume(self) -> None:
            while len(consumed) < 2:
                await asyncio.sleep(0)
            consumed.append("index.repo")
            raise _IndexStopped

    @asynccontextmanager
    async def index_consumer(
        settings: object,
        connection: object,
        session_factory: object,
        delays: object,
        transport: object = None,
    ) -> AsyncIterator[IndexConsumer]:
        composed.update(
            settings=settings,
            connection=connection,
            session_factory=session_factory,
            delays=delays,
            transport=transport,
        )
        yield IndexConsumer()

    async def wait_for_quiescence() -> None:
        return None

    process = SimpleNamespace(
        handle_run=SimpleNamespace(execute=None, wait_for_quiescence=wait_for_quiescence),
        handle_delivery=SimpleNamespace(execute=None),
        publish_review=SimpleNamespace(execute=None),
        leader_tick=None,
    )
    monkeypatch.setattr(worker, "amqp_channels", amqp_channels)
    monkeypatch.setattr(worker, "consume", consume)
    monkeypatch.setattr(worker, "run_as_leader", leader)
    monkeypatch.setattr(worker, "index_consumer", index_consumer)
    monkeypatch.setattr(worker, "compose_worker_process", lambda **kwargs: process)
    settings = worker.WorkerSettings.from_environment(_BASE_ENV)
    delays = RetryDelays(short=timedelta(seconds=1))
    transport = httpx.MockTransport(lambda request: httpx.Response(500))

    with pytest.raises(ExceptionGroup) as stopped:
        asyncio.run(worker.run_worker(settings, delays=delays, github_transport=transport))

    assert stopped.group_contains(_IndexStopped)
    assert consumed == ["review.run.fast", "review.publish", "index.repo"]
    # SD §13: a SQL error of a job logs no bound parameters (paths, symbol names).
    session_factory = composed.pop("session_factory")
    assert isinstance(session_factory, async_sessionmaker)
    assert session_factory.kw["bind"].sync_engine.hide_parameters is True
    assert composed == {
        "settings": settings,
        "connection": channels.connection,
        "delays": delays,
        "transport": transport,
    }


@pytest.mark.parametrize(("listing", "sizes"), [(False, True), (True, False)])
def test_actual_worker_source_forwarders_preserve_completeness_and_filter_files(
    listing: bool, sizes: bool
) -> None:
    from app.modules.reviews.application.conventions import (
        PolicyFilteredConventionsSource,
        RepositoryTreeResult,
        normalize_repository_tree,
    )
    from app.modules.reviews.application.handle_review_run import CheckpointedProvider
    from app.modules.reviews.infrastructure.provider_conventions import (
        ProviderRepositoryConventionsSource,
    )
    from tests.test_repo_conventions import REPOSITORY_ID, FakeSource

    class Source(FakeSource):
        async def fetch_tree(self, repository_id: UUID) -> RepositoryTreeResult:
            return RepositoryTreeResult(
                (RepositoryFile("src/app.py", 3), RepositoryFile("web/app.ts", 1)),
                listing,
                sizes,
                ("sizes_incomplete",),
            )

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
            return None

    async def checkpoint() -> None:
        raise AssertionError("tree forwarding does not add an LLM checkpoint")

    source = worker.AttemptReviewProvider(
        Source(), worker.UnconfiguredModel(), worker.UnconfiguredModel()
    )
    chain = PolicyFilteredConventionsSource(
        ProviderRepositoryConventionsSource(CheckpointedProvider(source, checkpoint)),
        lambda path: path.startswith("web/"),
    )
    result = normalize_repository_tree(asyncio.run(chain.fetch_tree(REPOSITORY_ID)))
    assert result.files == (RepositoryFile("src/app.py", 3),)
    assert (result.listing_complete, result.sizes_complete, result.incomplete_reasons) == (
        listing,
        sizes,
        ("sizes_incomplete",),
    )
