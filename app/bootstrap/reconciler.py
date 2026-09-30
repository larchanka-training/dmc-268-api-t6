"""Reconciler leader loop of portal-api (docs/PIPELINE_SPEC.md §1: T12, T13, T17, T18)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.infrastructure.db.leader import RECONCILER_LEADER_LOCK, run_as_leader
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)

_LOGGER = logging.getLogger(__name__)

RECONCILER_PERIOD_SECONDS = 5 * 60


@asynccontextmanager
async def reconciler_loop(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    rabbitmq_url: str | None,
    *,
    period: float = RECONCILER_PERIOD_SECONDS,
) -> AsyncIterator[None]:
    if not rabbitmq_url:
        _LOGGER.warning("RABBITMQ_URL is not set: the Run reconciler is disabled")
        yield
        return
    # The broker connects on the first republication; its outage never stops portal-api.
    publisher = LazyAmqpPublisher(rabbitmq_url)
    reconcile = ReconcileRuns(
        uow_factory=partial(SqlAlchemyRunLifecycleUnitOfWork, session_factory),
        run_publisher=publisher,
        review_queue=publisher,
    )
    task = asyncio.create_task(
        run_as_leader(engine, RECONCILER_LEADER_LOCK, period, reconcile.execute, name="reconciler")
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await publisher.aclose()
