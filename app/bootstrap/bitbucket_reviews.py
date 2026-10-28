"""Per-attempt Bitbucket review sources bound to persisted run origin authority."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.code_index.infrastructure.graph_retriever import SqlAlchemyGraphRetriever
from app.modules.code_index.infrastructure.review_index import SqlAlchemyReviewIndexUnitOfWork
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketRequestAuthority,
    BitbucketTargetInactive,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.bitbucket.infrastructure.models import BitbucketRunOrigin
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.integrations.bitbucket.infrastructure.run_http import BitbucketRunHttp
from app.modules.reviews.application.ast_resolution import FileUnavailable
from app.modules.reviews.application.bitbucket_status_intents import BitbucketStatusIntents
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.get_run_file_lines import BlobCache
from app.modules.reviews.application.graph_retriever import (
    GraphHints,
    GraphRetriever,
    GraphTarget,
)
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.policy_snapshot import ExecutionProfile
from app.modules.reviews.application.prepare_policy_snapshot import (
    PreparePolicySnapshot,
    RunPolicyPins,
)
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.rag_context import (
    BlobCacheUnitOfWork,
    BlobSource,
    CachedBlobSource,
)
from app.modules.reviews.application.rag_index import OpenReviewIndex
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.run_trace import ContextTrace
from app.modules.reviews.application.vcs_diff import is_generated_path
from app.modules.reviews.infrastructure.bitbucket_ci import (
    BitbucketBuildStatusPublisher,
    BitbucketStatusReconciler,
    SqlAlchemyBitbucketStatusSource,
)
from app.modules.reviews.infrastructure.bitbucket_policy_source import BitbucketPolicyDocumentSource
from app.modules.reviews.infrastructure.bitbucket_publication_resources import (
    BitbucketInstalledPublicationResources,
)
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree
from app.modules.reviews.infrastructure.bitbucket_revision_source import BitbucketRevisionSource
from app.modules.reviews.infrastructure.bitbucket_run_source import BitbucketRunSource
from app.modules.reviews.infrastructure.bitbucket_status_ledger import (
    SqlAlchemyBitbucketStatusUnitOfWork,
)
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    UNPINNED_EXECUTION_PROFILE,
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.redis_context_cache import (
    RedisAstFactsCache,
    RedisLike,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser


@dataclass(frozen=True)
class BitbucketAttemptBundle:
    vcs: HttpBitbucketVcsProvider
    source: BitbucketRunSource
    context_stage: ReviewContextStage


@dataclass(frozen=True)
class _RagFence:
    authority: BitbucketRequestAuthority
    target: BoundBitbucketTarget
    run_checkpoint: RunProviderCheckpoint

    async def check(self) -> None:
        await self.run_checkpoint()
        try:
            await self.authority.checkpoint(self.target)
        except (
            BitbucketTargetInactive,
            BitbucketCredentialGenerationChanged,
            BitbucketBindingGenerationChanged,
        ):
            raise RunCancelled("installation_unavailable") from None


class _FencedGraphRetriever:
    def __init__(self, *, inner: GraphRetriever, fence: _RagFence) -> None:
        self._inner = inner
        self._fence = fence

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        if repository_id != self._fence.target.repository_id:
            raise RunCancelled("installation_unavailable")
        await self._fence.check()
        hints = await self._inner.read_hints(repository_id, targets)
        await self._fence.check()
        return hints


class _FencedRagBlobs(CachedBlobSource):
    """A cached blob is still subject to the current run and Bitbucket binding fence."""

    def __init__(
        self,
        *,
        fence: _RagFence,
        repository_id: UUID,
        source: BlobSource,
        cache: BlobCache,
        uow_factory: Callable[[], BlobCacheUnitOfWork],
    ) -> None:
        super().__init__(
            repository_id=repository_id,
            source=source,
            cache=cache,
            uow_factory=uow_factory,
        )
        self._fence = fence

    async def text(self, blob_sha: str) -> str | FileUnavailable:
        await self._fence.check()
        result = await super().text(blob_sha)
        await self._fence.check()
        return result


class BitbucketReviewWorkerResources:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: BitbucketCredentialCipher,
        redis: RedisLike | None = None,
        execution_profile: ExecutionProfile = UNPINNED_EXECUTION_PROFILE,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._reader = SqlAlchemyBitbucketTargetReader(session_factory)
        self._credentials = BitbucketRequestCredentials(self._reader, cipher)
        self._redis = redis
        self._profile = execution_profile
        self._transports = transport_factory
        self.publications = BitbucketInstalledPublicationResources(
            session_factory=session_factory,
            cipher=cipher,
            transport_factory=transport_factory,
        )
        status_ledger = BitbucketStatusIntents(
            lambda: SqlAlchemyBitbucketStatusUnitOfWork(session_factory)
        )
        self.statuses = BitbucketBuildStatusPublisher(
            SqlAlchemyBitbucketStatusSource(session_factory),
            self._credentials,
            transport_factory=transport_factory,
            ledger=status_ledger,
        )
        self.status_reconciler = BitbucketStatusReconciler(status_ledger, self.statuses)

    async def _target(
        self, claimed: ClaimedAttempt, expected_input: RunVcsInput
    ) -> BoundBitbucketTarget:
        locator = expected_input.locator
        if (
            claimed.engine != "fast"
            or expected_input.provider != "bitbucket"
            or locator.repository_uuid is None
            or locator.origin_provider_installation_id is None
        ):
            raise RunCancelled("installation_unavailable")
        async with self._sessions() as session:
            origin = await session.scalar(
                select(BitbucketRunOrigin).where(BitbucketRunOrigin.run_id == claimed.run_id)
            )
        if (
            origin is None
            or origin.workspace_id != claimed.workspace_id
            or origin.provider_installation_id != locator.origin_provider_installation_id
            or origin.repository_id != expected_input.repository_id
            or origin.repository_uuid != locator.repository_uuid
            or origin.pr_number != locator.number
            or origin.head_sha != expected_input.head_sha
        ):
            raise RunCancelled("installation_unavailable")
        target = BoundBitbucketTarget(
            origin.provider_installation_id,
            origin.workspace_id,
            origin.repository_id,
            origin.repository_uuid,
            origin.credential_generation,
            origin.binding_generation,
        )
        try:
            await self._reader.checkpoint(target)
        except (
            BitbucketTargetInactive,
            BitbucketCredentialGenerationChanged,
            BitbucketBindingGenerationChanged,
        ):
            raise RunCancelled("installation_unavailable") from None
        return target

    @asynccontextmanager
    async def attempt(
        self,
        *,
        claimed: ClaimedAttempt,
        expected_input: RunVcsInput,
        checkpoint: RunProviderCheckpoint,
        trace: ContextTrace | None = None,
    ) -> AsyncIterator[BitbucketAttemptBundle]:
        await checkpoint()
        target = await self._target(claimed, expected_input)
        await checkpoint()
        async with httpx.AsyncClient(
            transport=self._transports() if self._transports else None,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            http = BitbucketRunHttp(client, self._credentials, target)
            vcs = HttpBitbucketVcsProvider(
                http=http,
                locator=expected_input.locator,
                pinned_base_sha=expected_input.base_sha,
                pinned_target_branch=expected_input.base_ref,
            )
            trees = BitbucketRepositoryTree(
                http=http, expected_input=expected_input, vcs=vcs, checkpoint=checkpoint
            )
            source = BitbucketRunSource(trees=trees, vcs=vcs, run_id=claimed.run_id)

            def policy_source(pins: RunPolicyPins) -> BitbucketPolicyDocumentSource:
                if (
                    pins.run_id != claimed.run_id
                    or pins.repository_id != expected_input.repository_id
                    or pins.policy_base_sha != expected_input.base_sha
                ):
                    raise RunCancelled("installation_unavailable")
                return BitbucketPolicyDocumentSource(
                    trees=trees, policy_base_sha=pins.policy_base_sha
                )

            def context_builder(run: RunVcsInput) -> BuildReviewContext:
                if run != expected_input:
                    raise RunCancelled("installation_unavailable")
                revision = BitbucketRevisionSource(trees=trees, locator=run.locator)
                fence = _RagFence(self._credentials, target, checkpoint)
                return BuildReviewContext(
                    source=revision,
                    parser=SubprocessSourceParser(),
                    counter=EstimatedTokenCounter(),
                    cache=None
                    if self._redis is None
                    else RedisAstFactsCache(self._redis, repository_id=run.repository_id),
                    builtin_excluded=is_generated_path,
                    retriever=_FencedGraphRetriever(
                        inner=SqlAlchemyGraphRetriever(self._sessions), fence=fence
                    ),
                    rag_blobs=_FencedRagBlobs(
                        repository_id=run.repository_id,
                        source=revision,
                        cache=SqlAlchemyBlobCache(self._sessions),
                        uow_factory=partial(SqlAlchemyRunProcessingUnitOfWork, self._sessions),
                        fence=fence,
                    ),
                )

            yield BitbucketAttemptBundle(
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
                    payloads=trace,
                    builtin_excluded=is_generated_path,
                    rag=OpenReviewIndex(partial(SqlAlchemyReviewIndexUnitOfWork, self._sessions)),
                ),
            )
