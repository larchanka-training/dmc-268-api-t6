"""Review-worker composition boundary.

The AMQP consumer is not implemented in this repository yet. Its process
lifecycle owns ``ReviewWorker``; each claimed message calls its
``process_review_run`` method without creating a new connection pool.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)
from app.modules.reviews.application.conventions import (
    GenerateRepoConventions,
)
from app.modules.reviews.application.execute_review import ExecuteReviewRun, ReviewModel
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunDiffProvider
from app.modules.reviews.application.review_output import PublishReviewOutput, ReviewProvider
from app.modules.reviews.application.vcs_diff import VcsProvider
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
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
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository

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
) -> bool:
    """Process one run using the worker's already-created database pool."""

    repository = SqlAlchemyRunRepository(session_factory)
    blob_cache = SqlAlchemyBlobCache(session_factory)
    conventions = GenerateRepoConventions(
        ProviderRepositoryConventionsSource(provider),
        ProviderConventionsModel(provider),
        lambda: SqlAlchemyRepositoryConventionsUnitOfWork(session_factory),
    )
    processor = ReviewRunProcessor(
        repository, provider, blob_cache, conventions, vcs_provider=vcs_provider
    )
    publisher = PublishReviewOutput(
        lambda: SqlAlchemyReviewOutputUnitOfWork(session_factory), provider
    )
    return await ExecuteReviewRun(
        processor,
        SqlAlchemyReviewPromptRepository(session_factory),
        provider,
        publisher,
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
