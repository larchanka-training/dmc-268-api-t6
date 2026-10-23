"""Review-worker composition root and process entrypoint.

Run as a separate process with ``uv run python -m app.worker``: it declares the
RabbitMQ topology, consumes ``review.run.fast`` and ``review.publish`` (prefetch 1
each) and runs the leader loop (no-CI sweep, outbox replay and the graph index's TTL
purge and garbage collection).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.bootstrap.code_index_worker import (
    CodeIndexSettings,
    index_consumer,
    index_leader_tasks,
)
from app.bootstrap.llm_gateway import build_gateway
from app.bootstrap.worker_shutdown import (
    finish_cleanup,
    run_background,
    run_with_signals,
    stop_during_startup,
    supervise_shutdown,
)
from app.common.infrastructure.db.leader import WORKER_LEADER_LOCK, run_as_leader
from app.common.infrastructure.heartbeat import beat, heartbeat_file, reset
from app.modules.code_index.application.leader_tasks import CodeIndexLeaderTasks
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)
from app.modules.integrations.webhooks.infrastructure.redis_installation_token_cache import (
    installation_token_cache,
)
from app.modules.reviews.application.check_runs import CheckRunGateway, RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    ConventionsModel,
    ConventionsRequest,
    GenerateRepoConventions,
    PolicyFilteredConventionsSource,
    RepositoryConventionsSource,
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
from app.modules.reviews.application.policy_documents import (
    PolicyDocumentSource,
    PolicyTreeEntry,
)
from app.modules.reviews.application.policy_snapshot import ExecutionProfile
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunDiffProvider,
    RunVcsInput,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
from app.modules.reviews.application.publish_cancellation_signals import (
    PublishCancellationSignals,
)
from app.modules.reviews.application.publish_run_review import (
    GitLabDiscussionGateway,
    PublishRunReview,
    PullRequestReviewGateway,
)
from app.modules.reviews.application.queue_messages import ReviewPublishQueue, RunRetryQueue
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
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
    RunCancelled,
    RunFailure,
)
from app.modules.reviews.application.run_trace import (
    RunTrace,
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
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsProvider,
    _is_generated,
)
from app.modules.reviews.infrastructure.amqp import (
    PUBLISH_QUEUE,
    amqp_channels,
    consume,
    handle_publish_delivery,
    handle_run_delivery,
)
from app.modules.reviews.infrastructure.amqp import run_queue as run_queue_name
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.branch_selection_rule import (
    SqlAlchemyBranchSelectionRule,
)
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.github_ci import HttpGitHubCurrentHeadCiProvider
from app.modules.reviews.infrastructure.github_policy_source import GitHubPolicyDocumentSource
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.github_review_publication import (
    GitHubCheckRunGateway,
    GitHubPullRequestReviewGateway,
)
from app.modules.reviews.infrastructure.github_revision_source import GitHubRevisionSource
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.github_vcs import (
    HttpGitHubVcsProvider,
    InstallationTokenProvider,
)
from app.modules.reviews.infrastructure.gitlab_review_publication import (
    GitLabMergeRequestDiscussionGateway,
)
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
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    UNPINNED_EXECUTION_PROFILE,
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.provider_conventions import (
    ProviderConventionsModel,
    ProviderRepositoryConventionsSource,
    ReviewConventionsProvider,
)
from app.modules.reviews.infrastructure.redis_context_cache import (
    RedisAstFactsCache,
    RedisLike,
    RedisTreeCache,
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
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
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
    context_stage: ReviewContextStage | None = None,
) -> bool:
    """Process one run using the worker's already-created database pool.

    The queue worker passes ``output`` (store and hand over to ``review.publish``) and
    ``trace`` (``run_actions`` steps); without them the answer is published inline.
    """

    repository = SqlAlchemyRunRepository(session_factory, allow_unscoped=True)
    blob_cache = SqlAlchemyBlobCache(session_factory)
    conventions_source: RepositoryConventionsSource = ProviderRepositoryConventionsSource(provider)
    if context_stage is not None:
        # The ignore policy applies to what the conventions model is shown as well.
        conventions_source = PolicyFilteredConventionsSource(
            conventions_source, context_stage.excluded
        )
    conventions = GenerateRepoConventions(
        conventions_source,
        ProviderConventionsModel(provider),
        lambda: SqlAlchemyRepositoryConventionsUnitOfWork(session_factory),
    )
    stage_options = {} if context_stage is None else {"context_stage": context_stage}
    processor = ReviewRunProcessor(
        repository,
        provider,
        blob_cache,
        conventions,
        vcs_provider=vcs_provider,
        trace=trace,
        uow_factory=lambda: SqlAlchemyRunProcessingUnitOfWork(session_factory),
        **stage_options,
    )
    publisher = output or PublishReviewOutput(
        lambda: SqlAlchemyReviewOutputUnitOfWork(session_factory), provider
    )
    prompts: ReviewPromptRepository = SqlAlchemyReviewPromptRepository(session_factory)
    if trace is not None:
        prompts = TracedReviewPromptRepository(prompts, trace, trace, engine=engine)
    if context_stage is None:
        # A caller without GitHub App adapters: the run is composed without the stage.
        return await ExecuteReviewRun(processor, prompts, provider, publisher).execute(run_id)
    return await ExecuteReviewRun(
        processor, prompts, provider, publisher, context_filter=context_stage
    ).execute(run_id)


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
    gitlab_token: str | None = None
    gitlab_base_url: str = "https://gitlab.com"
    redis_url: str | None = None
    code_index: CodeIndexSettings = CodeIndexSettings()

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
            gitlab_token=env.get("GITLAB_TOKEN") or None,
            gitlab_base_url=env.get("GITLAB_BASE_URL") or "https://gitlab.com",
            # Optional: the cache of AST facts and file trees of the context stage.
            redis_url=env.get("REDIS_URL") or None,
            code_index=CodeIndexSettings.from_environment(env),
        )


def run_url_factory(portal_url: str | None) -> Callable[[UUID], str | None]:
    if not portal_url:
        return lambda _: None
    base = portal_url.rstrip("/")
    return lambda run_id: f"{base}/runs/{run_id}"


class _EligibilityUnavailable:
    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
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
    context_stage: Callable[[RunTrace | None, RunProviderCheckpoint], ReviewContextStage] | None = (
        None
    )


def execution_profile_of(llm: LlmSettings | None) -> ExecutionProfile:
    """The model settings of this worker that a policy snapshot fixes for its run (RFS §7.4)."""
    if llm is None:
        return UNPINNED_EXECUTION_PROFILE
    fallback = llm.fallback
    return ExecutionProfile(
        profile_name="fast",
        review_model=llm.primary.model,
        verifier_model=llm.primary.model,
        fallback_review_model=None if fallback is None else fallback.model,
        fallback_verifier_model=None if fallback is None else fallback.model,
        primary_provider_route=llm.primary.provider,
        fallback_provider_route=None if fallback is None else fallback.provider,
        tokenizer_version="tokenizer/v1",
        counter_version="counter/v2",
        temperature="0.0",
        response_schema_version="review-output/v2",
        recovery_profile_version="recovery/v1",
        budget_profile_version="budget/v1",
    )


def review_context_stage_factory(
    *,
    client: httpx.AsyncClient,
    tokens: InstallationTokenProvider,
    vcs: HttpGitHubVcsProvider,
    runs: SqlAlchemyRunRepository,
    session_factory: async_sessionmaker[AsyncSession],
    redis: RedisLike | None,
    execution_profile: ExecutionProfile = UNPINNED_EXECUTION_PROFILE,
) -> Callable[[RunTrace | None, RunProviderCheckpoint], ReviewContextStage]:
    """Compose the policy and context stage; every run gets its own parser."""

    def stage(trace: RunTrace | None, checkpoint: RunProviderCheckpoint) -> ReviewContextStage:
        def context_builder(run: RunVcsInput) -> BuildReviewContext:
            return BuildReviewContext(
                source=GitHubRevisionSource(
                    client=client,
                    token_provider=tokens,
                    vcs=vcs,
                    locator=run.locator,
                    checkpoint=checkpoint,
                    tree_cache=None
                    if redis is None
                    else RedisTreeCache(redis, repository_id=run.repository_id),
                ),
                parser=SubprocessSourceParser(),
                counter=EstimatedTokenCounter(),
                cache=None
                if redis is None
                else RedisAstFactsCache(redis, repository_id=run.repository_id),
                builtin_excluded=_is_generated,
            )

        locators: dict[UUID, PullRequestLocator] = {}

        def _policy_source(run_id: UUID, policy_base_sha: str) -> PolicyDocumentSource:
            return _LazyPolicySource(
                runs=runs,
                run_id=run_id,
                build=lambda locator: GitHubPolicyDocumentSource(
                    client=client,
                    token_provider=tokens,
                    vcs=vcs,
                    locator=locator,
                    policy_base_sha=policy_base_sha,
                    checkpoint=checkpoint,
                ),
                locators=locators,
            )

        return ReviewContextStage(
            runs=runs,
            checkpoint=checkpoint,
            # The diff of the attempt is mandatory: its read failures get run error codes.
            vcs=ClassifiedVcsProvider(vcs),
            merge_base=vcs,
            prepare_policy=PreparePolicySnapshot(
                uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(
                    session_factory, execution_profile=execution_profile
                ),
                source_factory=lambda pins: _policy_source(pins.run_id, pins.policy_base_sha),
            ),
            context_builder=context_builder,
            trace=trace,
            builtin_excluded=_is_generated,
        )

    return stage


class _LazyPolicySource:
    """Bind the policy source to the repository of its run on the first read."""

    def __init__(
        self,
        *,
        runs: SqlAlchemyRunRepository,
        run_id: UUID,
        build: Callable[[PullRequestLocator], PolicyDocumentSource],
        locators: dict[UUID, PullRequestLocator],
    ) -> None:
        self._runs = runs
        self._run_id = run_id
        self._build = build
        self._locators = locators
        self._source: PolicyDocumentSource | None = None

    async def _bound(self) -> PolicyDocumentSource:
        if self._source is None:
            run = await self._runs.get_run_vcs_input(self._run_id)
            if run is None:
                raise RunCancelled("installation_unavailable")
            self._source = self._build(run.locator)
        return self._source

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        return await (await self._bound()).list_entries(tree_oid)

    async def read_blob(self, oid: str) -> bytes:
        return await (await self._bound()).read_blob(oid)


def github_adapters(
    settings: WorkerSettings,
    client: httpx.AsyncClient | None,
    session_factory: async_sessionmaker[AsyncSession],
    redis: RedisLike | None = None,
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
        cache=installation_token_cache(redis),
        now=time.time,
    )
    github_vcs = HttpGitHubVcsProvider(client=client, token_provider=tokens)
    vcs = ClassifiedVcsProvider(github_vcs)
    runs = SqlAlchemyRunRepository(session_factory, allow_unscoped=True)
    context_stage = review_context_stage_factory(
        client=client,
        tokens=tokens,
        vcs=github_vcs,
        runs=runs,
        session_factory=session_factory,
        redis=redis,
        execution_profile=execution_profile_of(settings.llm),
    )
    return GitHubAdapters(
        context_stage=context_stage,
        vcs=vcs,
        run_source=lambda run_id: GitHubRunSource(
            client=client,
            token_provider=tokens,
            vcs=vcs,
            runs=runs,
            run_id=run_id,
            conventions_from_base=True,
            merge_base=github_vcs,
        ),
        check_runs=GitHubCheckRunGateway(client=client, token_provider=tokens),
        reviews=GitHubPullRequestReviewGateway(client=client, token_provider=tokens),
        eligibility=DetermineCiEligibility(
            candidates=SqlAlchemyEligibilityCandidateStore(session_factory),
            ci=HttpGitHubCurrentHeadCiProvider(client=client, token_provider=tokens),
            own_app_id=int(settings.github_app_id),
        ),
    )


def gitlab_review_gateway(
    settings: WorkerSettings,
    client: httpx.AsyncClient | None,
) -> GitLabMergeRequestDiscussionGateway | None:
    """GitLab discussions when ``GITLAB_TOKEN`` is set. Unset leaves the run publishing."""
    token = settings.gitlab_token
    if not token or client is None:
        return None
    return GitLabMergeRequestDiscussionGateway(client=client, token=token)


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
    index_tasks: CodeIndexLeaderTasks | None = None

    async def leader_tick(self) -> None:
        failure: Exception | None = None
        try:
            if self.sweep is not None:
                await self.sweep.execute()
            await self.enqueue.replay_pending_publications()
        except Exception as error:
            failure = error
        # After the replay, even a failed one; the index tasks never raise and share one
        # deadline, so they do not hold up the next tick for long (#143). A
        # cancellation is not an `Exception`: it ends the tick before the index tasks.
        if self.index_tasks is not None:
            await self.index_tasks.tick()
        if failure is not None:
            raise failure


def compose_worker_process(
    *,
    settings: WorkerSettings,
    session_factory: async_sessionmaker[AsyncSession],
    queue: WorkerQueue,
    github: GitHubAdapters | None,
    gitlab_reviews: GitLabDiscussionGateway | None = None,
    gateway: LlmGateway | None = None,
    provider_factory: ProviderFactory | None = None,
    delays: RetryDelays | None = None,
    attempt_deadline: timedelta = FAST_ATTEMPT_DEADLINE,
    heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    index_tasks: CodeIndexLeaderTasks | None = None,
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
            context_stage=github.context_stage(trace, checkpoint) if github.context_stage else None,
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
            selection=SqlAlchemyBranchSelectionRule(session_factory),
            delays=delays,
            attempt_deadline=attempt_deadline,
            heartbeat_interval=heartbeat_interval,
            run_url=run_url,
            now=now,
        ),
        publish_review=PublishRunReview(
            uow_factory=lifecycle,
            reviews=github.reviews if github is not None else None,
            gitlab_reviews=gitlab_reviews,
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
        index_tasks=index_tasks,
    )


async def run_worker(
    settings: WorkerSettings,
    *,
    stop: asyncio.Event | None = None,
    provider_factory: ProviderFactory | None = None,
    delays: RetryDelays | None = None,
    leader_period: float = 30.0,
    github_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    fx_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Consume until cancelled or stopped; main owns the process signal deadline."""
    async with stop_during_startup(stop) as startup_complete:
        await _run_worker(
            settings,
            stop=stop,
            startup_complete=startup_complete,
            provider_factory=provider_factory,
            delays=delays,
            leader_period=leader_period,
            github_transport=github_transport,
            llm_transport=llm_transport,
            fx_transport=fx_transport,
        )


async def _run_worker(
    settings: WorkerSettings,
    *,
    stop: asyncio.Event | None,
    startup_complete: asyncio.Event,
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
    if stop is not None and stop.is_set():
        return
    if settings.heartbeat_file is not None:
        reset(settings.heartbeat_file)
    # SD §13: a SQL error in a log carries no bound parameters (paths, symbol names).
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
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
        gitlab_client = (
            await stack.enter_async_context(
                httpx.AsyncClient(base_url=settings.gitlab_base_url, timeout=10.0)
            )
            if settings.gitlab_token
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
        redis: Redis | None = None
        if settings.redis_url is not None:
            # Only a cache (context stage and installation tokens): the pool connects on
            # first use and a failure reads as a miss.
            redis = Redis.from_url(settings.redis_url)
            stack.push_async_callback(redis.aclose)
        async with amqp_channels(settings.rabbitmq_url, delays or RetryDelays()) as channels:
            # The index.repo consumer (#142): its own channels, parser and GitHub client;
            # None without the GitHub App.
            index = await stack.enter_async_context(
                index_consumer(
                    settings,
                    channels.connection,
                    session_factory,
                    delays or RetryDelays(),
                    github_transport,
                )
            )
            process = compose_worker_process(
                settings=settings,
                session_factory=session_factory,
                queue=channels.publisher,
                github=github_adapters(
                    settings, client, session_factory, cast(RedisLike | None, redis)
                ),
                gitlab_reviews=gitlab_review_gateway(settings, gitlab_client),
                gateway=gateway,
                provider_factory=provider_factory,
                delays=delays,
                # Converge, GC and the TTL purge touch only the database: on without the
                # GitHub App. The reconcile and the wake-up dispatch use the consumer.
                index_tasks=index_leader_tasks(settings.code_index, session_factory, index),
            )
            run_queue = await channels.consumer_queue(run_queue_name("fast"))
            publish_queue = await channels.consumer_queue(PUBLISH_QUEUE)
            _LOGGER.info("Review worker %s is consuming", settings.worker_id)
            startup_complete.set()
            if stop is not None and stop.is_set():
                return
            stop_options = {} if stop is None else {"stop": stop}
            try:
                async with asyncio.TaskGroup() as tasks:
                    consumers = [
                        tasks.create_task(
                            consume(
                                run_queue,
                                partial(
                                    handle_run_delivery,
                                    handler=process.handle_run.execute,
                                    publisher=channels.publisher,
                                ),
                                **stop_options,
                            )
                        ),
                        tasks.create_task(
                            consume(
                                publish_queue,
                                partial(
                                    handle_publish_delivery,
                                    handler=process.publish_review.execute,
                                    publisher=channels.publisher,
                                ),
                                **stop_options,
                            )
                        ),
                    ]
                    background = [
                        tasks.create_task(
                            run_background(
                                stop,
                                partial(
                                    run_as_leader,
                                    engine,
                                    WORKER_LEADER_LOCK,
                                    leader_period,
                                    process.leader_tick,
                                    name="worker",
                                ),
                            )
                        )
                    ]
                    if index is not None:
                        consumers.append(tasks.create_task(index.consume(**stop_options)))
                    # Beats only once the consumers are attached; any failed task ends the group.
                    if settings.heartbeat_file is not None:
                        background.append(
                            tasks.create_task(
                                run_background(stop, partial(beat, settings.heartbeat_file))
                            )
                        )
                    if stop is not None:
                        tasks.create_task(supervise_shutdown(stop, consumers, background))
            finally:
                # A normal watchdog/failure can finish a delivery while its cancelled
                # pipeline still runs. Join every owned phase before resource contexts
                # close, including TaskGroup fatal/caller-cancellation exits.
                await finish_cleanup(
                    process.handle_run.wait_for_quiescence(), prior_error=sys.exception()
                )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_with_signals(
        lambda stop: run_worker(WorkerSettings.from_environment(os.environ), stop=stop)
    )


if __name__ == "__main__":
    main()
