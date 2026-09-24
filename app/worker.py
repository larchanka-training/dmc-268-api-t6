"""Review-worker composition boundary.

The AMQP consumer is not implemented in this repository yet. Its process
lifecycle owns ``ReviewWorker``; each claimed message calls its
``process_review_run`` method without creating a new connection pool.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.reviews.application.process_run import ReviewRunProcessor, RunDiffProvider
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository


async def process_review_run(
    run_id: UUID,
    provider: RunDiffProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Process one run using the worker's already-created database pool."""

    repository = SqlAlchemyRunRepository(session_factory)
    return await ReviewRunProcessor(repository, provider).execute(run_id)


class ReviewWorker:
    def __init__(
        self,
        provider: RunDiffProvider,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._engine = engine

    async def process_review_run(self, run_id: UUID) -> bool:
        return await process_review_run(run_id, self._provider, self._session_factory)

    async def aclose(self) -> None:
        await self._engine.dispose()


@asynccontextmanager
async def review_worker(
    provider: RunDiffProvider, database_url: str | None = None
) -> AsyncIterator[ReviewWorker]:
    """Compose a worker once and dispose its pool during worker shutdown."""

    url = database_url or os.environ.get("DATABASE_URL")
    if url is None:
        raise RuntimeError("DATABASE_URL must be configured to process a review run")
    engine = create_async_engine(url, pool_pre_ping=True)
    worker = ReviewWorker(provider, async_sessionmaker(engine, expire_on_commit=False), engine)
    try:
        yield worker
    finally:
        await worker.aclose()
