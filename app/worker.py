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
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.bootstrap.llm_gateway import build_gateway
from app.common.infrastructure.db.leader import WORKER_LEADER_LOCK, run_as_leader
from app.common.infrastructure.heartbeat import beat, heartbeat_file, reset
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)
from app.modules.reviews.application.check_runs import CheckRunGateway
from app.modules.reviews.application.conventions import (
    ConventionsModel,
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
from app.modules.reviews.application.llm import RunCallContext
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunDiffProvider
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
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
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.llm.ecb_fx import EcbFxQuoteCache, EcbFxRateAdapter
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.no_ci_sweep_candidates import (
    SqlAlchemySweepNoCiUnitOfWork,
)
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
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
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

    try:
        repository = SqlAlchemyRunRepository(session_factory, allow_unscoped=True)
    except TypeError:
        repository = SqlAlchemyRunRepository(session_factory)
    blob_cache = SqlAlchemyBlobCache(session_factory)
    conventions = GenerateRepoConventions(
        ProviderRepositoryConventionsSource(provider),
        ProviderConventionsModel(provider),
        lambda: SqlAlchemyRepositoryConventionsUnitOfWork(session_factory),
    )
    processor = ReviewRunProcessor(
        repository,
        provider,
        blob_cache,
        conventions,
        vcs_provider=vcs_provider,
        trace=trace,
        uow_factory=lambda: SqlAlchemyRunProcessingUnitOfWork(session_factory),
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


LLM_NOT_CONFIGURED = (
    "the LLM gateway is not configured: set LLM_MODEL and LLM_API_KEYS "
    "(or LLM_BASE_URL for a self-hosted model)"
)


class UnconfiguredModel:
    """Model calls of a worker started without ``LLM_*``: the Run fails and says why."""

    async def draft_conventions(self, *, request: ConventionsRequest) -> Mapping[str, object]:
        raise RunFailure("llm_unavailable", LLM_NOT_CONFIGURED)

    async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object] | str | bytes:
        raise RunFailure("llm_unavailable", LLM_NOT_CONFIGURED)


class AttemptReviewProvider:
    """One attempt's provider: GitHub reads of the Run plus the models for the attempt."""

    def __init__(
        self,
        source: GitHubRunSource,
        conventions: ConventionsModel,
        review: GatewayReviewModel | UnconfiguredModel,
    ) -> None:
        self._source = source
        self._conventions = conventions
        self._review = review

    async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
        raise RuntimeError("the worker reads the diff through its VcsProvider")

    async def fetch_file_content(self, *, code_change_id: UUID, head_sha: str, path: str) -> str:
        raise RuntimeError("the worker reads files through its VcsProvider")

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return await self._source.fetch_agents_md(repository_id)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return await self._source.fetch_tree(repository_id)

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return await self._source.fetch_files(repository_id, paths)

    async def draft_conventions(self, *, request: ConventionsRequest) -> Mapping[str, object]:
        return await self._conventions.draft_conventions(request=request)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        return await self._source.get_pull_request_meta(run_id)

    async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object] | str | bytes:
        return await self._review.draft_review(context=context)

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        raise RuntimeError("the review is published by the review.publish consumer")


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
    heartbeat_file: Path | None = None
    llm: LlmSettings | None = None

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

        database_url = required("DATABASE_URL")
        rabbitmq_url = required("RABBITMQ_URL")
        llm = LlmSettings.from_env(env) if env.get("LLM_MODEL") else None

        return cls(
            database_url=database_url,
            rabbitmq_url=rabbitmq_url,
            worker_id=env.get("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}",
            github_app_id=env.get("GITHUB_APP_ID") or None,
            github_private_key=env.get("GITHUB_APP_PRIVATE_KEY") or None,
            github_api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
            portal_url=env.get("PORTAL_URL") or None,
            heartbeat_file=heartbeat_file(env),
            # Partly set LLM_* fails the start; none at all starts with a warning.
            llm=llm,
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
    run_source: Callable[[UUID], GitHubRunSource]


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
    vcs = ClassifiedVcsProvider(HttpGitHubVcsProvider(client=client, token_provider=tokens))
    runs = SqlAlchemyRunRepository(session_factory)
    return GitHubAdapters(
        vcs=vcs,
        run_source=lambda run_id: GitHubRunSource(
            client=client, token_provider=tokens, vcs=vcs, runs=runs, run_id=run_id
        ),
        check_runs=GitHubCheckRunGateway(client=client, token_provider=tokens),
        reviews=GitHubPullRequestReviewGateway(client=client, token_provider=tokens),
        eligibility=DetermineCiEligibility(
            candidates=SqlAlchemyEligibilityCandidateStore(session_factory),
            ci=HttpGitHubCurrentHeadCiProvider(client=client, token_provider=tokens),
            own_app_id=int(settings.github_app_id),
        ),
    )


def attempt_provider_factory(
    gateway: LlmGateway | None, github: GitHubAdapters | None
) -> ProviderFactory:
    """The production provider of each attempt: models bound to its ``RunCallContext``."""

    def factory(claimed: ClaimedAttempt) -> ReviewWorkerProvider:
        if github is None:
            raise RunFailure("github_forbidden", "the GitHub App is not configured")
        source = github.run_source(claimed.run_id)
        if gateway is None:
            return AttemptReviewProvider(source, UnconfiguredModel(), UnconfiguredModel())
        run = RunCallContext(
            run_id=claimed.run_id,
            workspace_id=claimed.workspace_id,
            attempt=claimed.attempt,
            engine=claimed.engine,
            deadline=claimed.deadline,
            prompt_version_id=claimed.prompt_version_id,
            rule_version_id=claimed.rule_version_id,
        )
        return AttemptReviewProvider(
            source,
            GatewayConventionsModel(gateway, run),
            GatewayReviewModel(gateway, run, source),
        )

    return factory


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
    gateway: LlmGateway | None = None,
    provider_factory: ProviderFactory | None = None,
    delays: RetryDelays | None = None,
    attempt_deadline: timedelta = FAST_ATTEMPT_DEADLINE,
    heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> WorkerProcess:
    factory = provider_factory or attempt_provider_factory(gateway, github)
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
            SweepNoCi(
                uow_factory=lambda: SqlAlchemySweepNoCiUnitOfWork(session_factory),
                enqueuer=enqueue,
            )
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
    github_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    fx_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Consume until cancelled; the caller owns signal handling.

    The transports replace the network in tests of this very composition.
    """
    if settings.heartbeat_file is not None:
        reset(settings.heartbeat_file)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        client = (
            await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=settings.github_api_url, timeout=10.0, transport=github_transport
                )
            )
            if settings.github_app_configured
            else None
        )
        gateway: LlmGateway | None = None
        if settings.llm is None:
            _LOGGER.warning("LLM_MODEL is not set: %s; every review run fails", LLM_NOT_CONFIGURED)
        else:
            # One gateway and separate LLM/ECB HTTP pools per process.
            llm_client = await stack.enter_async_context(httpx.AsyncClient(transport=llm_transport))
            fx_client = await stack.enter_async_context(httpx.AsyncClient(transport=fx_transport))
            gateway = build_gateway(
                settings.llm,
                llm_client,
                session_factory,
                fx_provider=EcbFxQuoteCache(EcbFxRateAdapter(fx_client)),
            )
        async with amqp_channels(settings.rabbitmq_url, delays or RetryDelays()) as channels:
            process = compose_worker_process(
                settings=settings,
                session_factory=session_factory,
                queue=channels.publisher,
                github=github_adapters(settings, client, session_factory),
                gateway=gateway,
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
                # Beats only once both consumers are attached; any failed task ends the group.
                if settings.heartbeat_file is not None:
                    tasks.create_task(beat(settings.heartbeat_file))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(WorkerSettings.from_environment(os.environ)))


if __name__ == "__main__":
    main()
