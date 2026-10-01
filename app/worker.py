"""Review-worker composition root and process entrypoint.

Run as a separate process with ``uv run python -m app.worker``: it declares the
RabbitMQ topology, consumes ``review.run.fast`` and ``review.publish`` (prefetch 1
each) and runs the leader loop (no-CI sweep and outbox replay).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import NoReturn, Protocol
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.common.infrastructure.db.leader import WORKER_LEADER_LOCK, run_as_leader
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)
from app.modules.reviews.application.check_runs import CheckRunGateway
from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    GenerateRepoConventions,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    DetermineCiEligibility,
)
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewModel,
    ReviewPromptRepository,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.handle_review_run import (
    AttemptCheckpoint,
    CheckpointedProvider,
    ClaimedAttempt,
    HandleReviewRun,
)
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunDiffProvider
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.publish_cancellation_signals import (
    PublishCancellationSignals,
)
from app.modules.reviews.application.publish_run_review import (
    PublishRunReview,
    PullRequestReviewGateway,
)
from app.modules.reviews.application.queue_messages import ReviewPublishQueue, RunRetryQueue
from app.modules.reviews.application.review_output import (
    PublishedFinding,
    PublishReviewOutput,
    ReviewOutputHandler,
    ReviewProvider,
)
from app.modules.reviews.application.run_failures import (
    FAST_ATTEMPT_DEADLINE,
    HEARTBEAT_INTERVAL,
    RetryDelays,
    RunFailure,
)
from app.modules.reviews.application.run_trace import (
    TracedReviewPromptRepository,
    TransactionalRunTrace,
)
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.application.sweep_no_ci import SweepNoCi
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EligibilityChecker,
    RunMessagePublisher,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.application.vcs_diff import VcsProvider
from app.modules.reviews.infrastructure.amqp import (
    PUBLISH_QUEUE,
    amqp_channels,
    consume,
    handle_publish_delivery,
    handle_run_delivery,
)
from app.modules.reviews.infrastructure.amqp import run_queue as run_queue_name
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.github_ci import HttpGitHubCurrentHeadCiProvider
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.github_review_publication import (
    GitHubCheckRunGateway,
    GitHubPullRequestReviewGateway,
)
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.no_ci_sweep_candidates import SqlAlchemyDueNoCiCandidates
from app.modules.reviews.infrastructure.provider_conventions import (
    ProviderConventionsModel,
    ProviderRepositoryConventionsSource,
    ReviewConventionsProvider,
)
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.review_prompt_repository import (
    SqlAlchemyReviewPromptRepository,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.vcs_failures import ClassifiedVcsProvider
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork

_LOGGER = logging.getLogger(__name__)


class ReviewWorkerProvider(
    RunDiffProvider, ReviewConventionsProvider, ReviewModel, ReviewProvider, Protocol
):
    """All provider calls required by the ordinary review-worker pipeline."""


async def process_review_run(
    run_id: UUID,
    provider: ReviewWorkerProvider,
    session_factory: async_sessionmaker[AsyncSession],
    vcs_provider: VcsProvider,
    *,
    output: ReviewOutputHandler | None = None,
    trace: TransactionalRunTrace | None = None,
    engine: str = "fast",
) -> bool:
    """Process one run using the worker's already-created database pool.

    The queue worker passes ``output`` (store and hand over to ``review.publish``) and
    ``trace`` (``run_actions`` steps); without them the answer is published inline.
    """

    repository = SqlAlchemyRunRepository(session_factory)
    blob_cache = SqlAlchemyBlobCache(session_factory)
    conventions = GenerateRepoConventions(
        ProviderRepositoryConventionsSource(provider),
        ProviderConventionsModel(provider),
        lambda: SqlAlchemyRepositoryConventionsUnitOfWork(session_factory),
    )
    processor = ReviewRunProcessor(
        repository, provider, blob_cache, conventions, vcs_provider=vcs_provider, trace=trace
    )
    publisher = output or PublishReviewOutput(
        lambda: SqlAlchemyReviewOutputUnitOfWork(session_factory), provider
    )
    prompts: ReviewPromptRepository = SqlAlchemyReviewPromptRepository(session_factory)
    if trace is not None:
        prompts = TracedReviewPromptRepository(prompts, trace, trace, engine=engine)
    return await ExecuteReviewRun(processor, prompts, provider, publisher).execute(run_id)


class ReviewWorker:
    def __init__(
        self,
        provider: ReviewWorkerProvider,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        vcs_provider: VcsProvider | None,
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._engine = engine
        self._vcs_provider = vcs_provider

    async def process_review_run(self, run_id: UUID) -> bool:
        if self._vcs_provider is None:
            raise RuntimeError("GitHub VCS provider is unavailable for review processing")
        return await process_review_run(
            run_id, self._provider, self._session_factory, self._vcs_provider
        )

    async def aclose(self) -> None:
        await self._engine.dispose()


@asynccontextmanager
async def review_worker(
    provider: ReviewWorkerProvider,
    database_url: str | None = None,
    *,
    vcs_provider: VcsProvider | None = None,
) -> AsyncIterator[ReviewWorker]:
    """Compose a worker once and dispose its pool during worker shutdown."""

    url = database_url or os.environ.get("DATABASE_URL")
    if url is None:
        raise RuntimeError("DATABASE_URL must be configured to process a review run")
    async with AsyncExitStack() as stack:
        if vcs_provider is None:
            app_id = os.environ.get("GITHUB_APP_ID")
            private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
            if app_id and private_key:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                        timeout=10.0,
                    )
                )
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id=app_id,
                    private_key=private_key,
                    cache=InMemoryInstallationAccessTokenCache(now=time.time),
                    now=time.time,
                )
                vcs_provider = HttpGitHubVcsProvider(client=client, token_provider=tokens)
            else:
                _LOGGER.warning("GitHub VCS provider is unavailable; review runs will fail closed")
        engine = create_async_engine(url, pool_pre_ping=True)
        worker = ReviewWorker(
            provider, async_sessionmaker(engine, expire_on_commit=False), engine, vcs_provider
        )
        try:
            yield worker
        finally:
            await worker.aclose()


class UnavailableReviewProvider:
    """Placeholder until the LLM gateway (#33) is composed: every call is ``llm_unavailable``."""

    async def _unavailable(self) -> NoReturn:
        raise RunFailure("llm_unavailable", "no ReviewModel is configured for this worker")

    async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
        await self._unavailable()

    async def fetch_file_content(self, *, code_change_id: UUID, head_sha: str, path: str) -> str:
        await self._unavailable()

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        await self._unavailable()

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        await self._unavailable()

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        await self._unavailable()

    async def draft_conventions(self, *, request: ConventionsRequest) -> Mapping[str, object]:
        await self._unavailable()

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        await self._unavailable()

    async def draft_review(self, *, prompt: str) -> Mapping[str, object] | str | bytes:
        await self._unavailable()

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        await self._unavailable()


type ProviderFactory = Callable[[ClaimedAttempt], ReviewWorkerProvider]


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    rabbitmq_url: str
    worker_id: str
    github_app_id: str | None
    github_private_key: str | None
    github_api_url: str
    portal_url: str | None

    @property
    def github_app_configured(self) -> bool:
        return bool(self.github_app_id and self.github_private_key)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> WorkerSettings:
        def required(name: str) -> str:
            value = env.get(name)
            if not value:
                raise RuntimeError(f"{name} is required for the review worker")
            return value

        return cls(
            database_url=required("DATABASE_URL"),
            rabbitmq_url=required("RABBITMQ_URL"),
            worker_id=env.get("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}",
            github_app_id=env.get("GITHUB_APP_ID") or None,
            github_private_key=env.get("GITHUB_APP_PRIVATE_KEY") or None,
            github_api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
            portal_url=env.get("PORTAL_URL") or None,
        )


def run_url_factory(portal_url: str | None) -> Callable[[UUID], str | None]:
    if not portal_url:
        return lambda _: None
    base = portal_url.rstrip("/")
    return lambda run_id: f"{base}/runs/{run_id}"


class _EligibilityUnavailable:
    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> CiEligibility:
        raise RuntimeError("CI eligibility needs the GitHub App")


class WorkerQueue(RunMessagePublisher, RunRetryQueue, ReviewPublishQueue, Protocol):
    """All confirmed publications of the worker process."""


@dataclass(frozen=True)
class GitHubAdapters:
    vcs: VcsProvider
    check_runs: CheckRunGateway
    reviews: PullRequestReviewGateway
    eligibility: EligibilityChecker


def github_adapters(
    settings: WorkerSettings,
    client: httpx.AsyncClient | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> GitHubAdapters | None:
    """GitHub App adapters, or ``None`` with a warning when the App is not configured."""
    if not settings.github_app_configured or client is None:
        _LOGGER.warning(
            "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set: the no-CI sweep and "
            "GitHub publication (reviews and check-runs) are disabled"
        )
        return None
    assert settings.github_app_id is not None and settings.github_private_key is not None
    tokens = GitHubAppInstallationAccessTokenProvider(
        client=client,
        app_id=settings.github_app_id,
        private_key=settings.github_private_key,
        cache=InMemoryInstallationAccessTokenCache(now=time.time),
        now=time.time,
    )
    return GitHubAdapters(
        vcs=ClassifiedVcsProvider(HttpGitHubVcsProvider(client=client, token_provider=tokens)),
        check_runs=GitHubCheckRunGateway(client=client, token_provider=tokens),
        reviews=GitHubPullRequestReviewGateway(client=client, token_provider=tokens),
        eligibility=DetermineCiEligibility(
            candidates=SqlAlchemyEligibilityCandidateStore(session_factory),
            ci=HttpGitHubCurrentHeadCiProvider(client=client, token_provider=tokens),
            own_app_id=int(settings.github_app_id),
        ),
    )


@dataclass(frozen=True)
class WorkerProcess:
    """Everything the worker process runs; built without a broker connection for tests."""

    handle_run: HandleReviewRun
    publish_review: PublishRunReview
    enqueue: TryEnqueueWebhookRun
    sweep: SweepNoCi | None

    async def leader_tick(self) -> None:
        if self.sweep is not None:
            await self.sweep.execute()
        await self.enqueue.replay_pending_publications()


def compose_worker_process(
    *,
    settings: WorkerSettings,
    session_factory: async_sessionmaker[AsyncSession],
    queue: WorkerQueue,
    github: GitHubAdapters | None,
    provider_factory: ProviderFactory | None = None,
    delays: RetryDelays | None = None,
    attempt_deadline: timedelta = FAST_ATTEMPT_DEADLINE,
    heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> WorkerProcess:
    factory = provider_factory or (lambda _: UnavailableReviewProvider())
    run_url = run_url_factory(settings.portal_url)
    lifecycle = partial(SqlAlchemyRunLifecycleUnitOfWork, session_factory)
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, session_factory))

    async def pipeline(claimed: ClaimedAttempt) -> bool:
        if github is None:
            raise RunFailure("github_forbidden", "the GitHub App is not configured")
        checkpoint = AttemptCheckpoint(lifecycle, claimed.run_id, claimed.deadline, now)
        output = StoreReviewOutput(
            partial(SqlAlchemyReviewOutputUnitOfWork, session_factory),
            queue,
            worker_id=claimed.worker_id,
            now=now,
        )
        return await process_review_run(
            claimed.run_id,
            CheckpointedProvider(factory(claimed), checkpoint),
            session_factory,
            github.vcs,
            output=output,
            trace=trace,
            engine=claimed.engine,
        )

    enqueue = TryEnqueueWebhookRun(
        eligibility=github.eligibility if github is not None else _EligibilityUnavailable(),
        uow_factory=partial(SqlAlchemyWebhookRunUnitOfWork, session_factory),
        publisher=queue,
        cancellation_signals=PublishCancellationSignals(
            uow_factory=partial(SqlAlchemyPullRequestProjectionUnitOfWork, session_factory),
            publisher=queue,
        ),
    )
    return WorkerProcess(
        handle_run=HandleReviewRun(
            uow_factory=lifecycle,
            pipeline=pipeline,
            retry_queue=queue,
            check_runs=github.check_runs if github is not None else None,
            worker_id=settings.worker_id,
            delays=delays,
            attempt_deadline=attempt_deadline,
            heartbeat_interval=heartbeat_interval,
            run_url=run_url,
            now=now,
        ),
        publish_review=PublishRunReview(
            uow_factory=lifecycle,
            reviews=github.reviews if github is not None else None,
            check_runs=github.check_runs if github is not None else None,
            trace=trace,
            run_url=run_url,
            now=now,
        ),
        enqueue=enqueue,
        sweep=(
            SweepNoCi(candidates=SqlAlchemyDueNoCiCandidates(session_factory), enqueuer=enqueue)
            if github is not None
            else None
        ),
    )


async def run_worker(
    settings: WorkerSettings,
    *,
    provider_factory: ProviderFactory | None = None,
    delays: RetryDelays | None = None,
    leader_period: float = 30.0,
) -> None:
    """Consume until cancelled; the caller owns signal handling."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        client = (
            await stack.enter_async_context(
                httpx.AsyncClient(base_url=settings.github_api_url, timeout=10.0)
            )
            if settings.github_app_configured
            else None
        )
        async with amqp_channels(settings.rabbitmq_url, delays or RetryDelays()) as channels:
            process = compose_worker_process(
                settings=settings,
                session_factory=session_factory,
                queue=channels.publisher,
                github=github_adapters(settings, client, session_factory),
                provider_factory=provider_factory,
                delays=delays,
            )
            run_queue = await channels.consumer_queue(run_queue_name("fast"))
            publish_queue = await channels.consumer_queue(PUBLISH_QUEUE)
            _LOGGER.info("Review worker %s is consuming", settings.worker_id)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    consume(
                        run_queue,
                        partial(handle_run_delivery, handler=process.handle_run.execute),
                    )
                )
                tasks.create_task(
                    consume(
                        publish_queue,
                        partial(handle_publish_delivery, handler=process.publish_review.execute),
                    )
                )
                tasks.create_task(
                    run_as_leader(
                        engine,
                        WORKER_LEADER_LOCK,
                        leader_period,
                        process.leader_tick,
                        name="worker",
                    )
                )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(WorkerSettings.from_environment(os.environ)))


if __name__ == "__main__":
    main()
