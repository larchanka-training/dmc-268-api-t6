"""Installed GitLab review composition: stateless process resources, owned attempts."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.request_credentials import (
    GitLabRequestAuthority,
    GitLabRequestCredentials,
)
from app.modules.integrations.gitlab.infrastructure.connection_store import CredentialCipherAdapter
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.integrations.gitlab.infrastructure.run_credentials import GitLabRunCredentials
from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttp
from app.modules.integrations.gitlab.infrastructure.settings import GitLabRuntimeSettings
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.policy_snapshot import ExecutionProfile
from app.modules.reviews.application.prepare_policy_snapshot import (
    PreparePolicySnapshot,
    RunPolicyPins,
)
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.run_trace import ContextTrace
from app.modules.reviews.application.vcs_diff import is_generated_path
from app.modules.reviews.infrastructure.gitlab_policy_source import GitLabPolicyDocumentSource
from app.modules.reviews.infrastructure.gitlab_publication_resources import (
    GitLabInstalledPublicationResources,
)
from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree
from app.modules.reviews.infrastructure.gitlab_revision_source import GitLabRevisionSource
from app.modules.reviews.infrastructure.gitlab_run_source import GitLabRunSource
from app.modules.reviews.infrastructure.gitlab_vcs import HttpGitLabVcsProvider
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    UNPINNED_EXECUTION_PROFILE,
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.redis_context_cache import (
    RedisAstFactsCache,
    RedisLike,
    RedisTreeCache,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser


@dataclass(frozen=True)
class GitLabAttemptBundle:
    vcs: HttpGitLabVcsProvider
    source: GitLabRunSource
    context_stage: ReviewContextStage


class GitLabReviewWorkerResources:
    def __init__(
        self,
        *,
        settings: GitLabRuntimeSettings,
        session_factory: async_sessionmaker[AsyncSession],
        authority: GitLabRequestAuthority,
        ssl_context: ssl.SSLContext,
        redis: RedisLike | None,
        execution_profile: ExecutionProfile,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._authority = authority
        self._tls = ssl_context
        self._redis = redis
        self._profile = execution_profile
        self._transports = transport_factory
        self.publications = GitLabInstalledPublicationResources(
            session_factory=session_factory,
            authority=authority,
            origin_policy=settings.origin_policy,
            ssl_context=ssl_context,
            transport_factory=transport_factory,
        )

    @asynccontextmanager
    async def attempt(
        self,
        *,
        claimed: ClaimedAttempt,
        expected_input: RunVcsInput,
        checkpoint: RunProviderCheckpoint,
        trace: ContextTrace | None = None,
    ) -> AsyncIterator[GitLabAttemptBundle]:
        credentials = GitLabRunCredentials(
            session_factory=self._sessions,
            authority=self._authority,
            run_id=claimed.run_id,
            expected_workspace_id=claimed.workspace_id,
            expected_input=expected_input,
            checkpoint=checkpoint,
        )
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=self._settings.origin_policy,
            ssl_context=self._tls,
            transport=self._transports() if self._transports else None,
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            trees = GitLabRepositoryTree(http=http, checkpoint=checkpoint)
            source = GitLabRunSource(
                trees=trees, vcs=vcs, run_id=claimed.run_id, conventions_from_base=True
            )

            def policy_source(pins: RunPolicyPins) -> GitLabPolicyDocumentSource:
                if (
                    pins.run_id != claimed.run_id
                    or pins.repository_id != expected_input.repository_id
                    or pins.policy_base_sha != expected_input.base_sha
                ):
                    raise RunCancelled("installation_unavailable")
                return GitLabPolicyDocumentSource(trees=trees, policy_base_sha=pins.policy_base_sha)

            def context_builder(run: RunVcsInput) -> BuildReviewContext:
                if run != expected_input:
                    raise RunCancelled("installation_unavailable")
                return BuildReviewContext(
                    source=GitLabRevisionSource(
                        trees=trees,
                        locator=run.locator,
                        tree_cache=None
                        if self._redis is None
                        else RedisTreeCache(self._redis, repository_id=run.repository_id),
                    ),
                    parser=SubprocessSourceParser(),
                    counter=EstimatedTokenCounter(),
                    cache=None
                    if self._redis is None
                    else RedisAstFactsCache(self._redis, repository_id=run.repository_id),
                    builtin_excluded=is_generated_path,
                )

            yield GitLabAttemptBundle(
                vcs,
                source,
                ReviewContextStage(
                    runs=SqlAlchemyRunRepository(self._sessions, allow_unscoped=True),
                    checkpoint=checkpoint,
                    vcs=vcs,
                    merge_base=vcs,
                    prepare_policy=PreparePolicySnapshot(
                        uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(
                            self._sessions, execution_profile=self._profile
                        ),
                        source_factory=policy_source,
                    ),
                    context_builder=context_builder,
                    trace=trace,
                    builtin_excluded=is_generated_path,
                    payloads=trace,
                ),
            )


def build_gitlab_review_worker_resources(
    *,
    settings: GitLabRuntimeSettings,
    session_factory: async_sessionmaker[AsyncSession],
    redis: RedisLike | None = None,
    execution_profile: ExecutionProfile = UNPINNED_EXECUTION_PROFILE,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> GitLabReviewWorkerResources:
    try:
        tls = ssl.create_default_context(cafile=settings.ssl_cert_file)
        codec = CredentialCipherAdapter(
            CredentialCipher(active_key_id=settings.active_key_id, keys=settings.keys)
        )
    except (ValueError, OSError):
        raise RuntimeError("GitLab review worker configuration is invalid") from None
    return GitLabReviewWorkerResources(
        settings=settings,
        session_factory=session_factory,
        authority=GitLabRequestCredentials(SqlAlchemyGitLabTargetReader(session_factory), codec),
        ssl_context=tls,
        redis=redis,
        execution_profile=execution_profile,
        transport_factory=transport_factory,
    )
