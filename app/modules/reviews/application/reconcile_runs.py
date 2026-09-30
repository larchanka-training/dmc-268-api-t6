"""Reconciler of the portal-api leader loop: T12, T13, T17 and T18 (docs/PIPELINE_SPEC.md §1)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.handle_review_run import (
    RunLifecycleStore,
    RunLifecycleUnitOfWork,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer, ReviewPublishQueue
from app.modules.reviews.application.run_failures import MAX_ATTEMPTS, RECONCILER_QUEUED_GRACE
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunMessagePublisher,
)

_LOGGER = logging.getLogger(__name__)


class ReconcilerStore(RunLifecycleStore, Protocol):
    async def expired_running(self, now: datetime, limit: int) -> tuple[tuple[UUID, int], ...]: ...

    async def expired_publishing(
        self, now: datetime, limit: int
    ) -> tuple[ReviewPublishPointer, ...]: ...

    async def stale_queued(
        self, available_before: datetime, limit: int
    ) -> tuple[PendingRunMessage, ...]: ...


class ReconcilerUnitOfWork(RunLifecycleUnitOfWork, Protocol):
    @property
    def runs(self) -> ReconcilerStore: ...


class ReconcileRuns:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], ReconcilerUnitOfWork],
        run_publisher: RunMessagePublisher,
        review_queue: ReviewPublishQueue,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._run_publisher = run_publisher
        self._review_queue = review_queue
        self._now = now

    async def execute(self, *, limit: int = 100) -> int:
        """Return how many Runs were republished."""
        now = self._now()
        async with self._uow_factory() as uow:
            running = await uow.runs.expired_running(now, limit)
            publishing = await uow.runs.expired_publishing(now, limit)
            queued = await uow.runs.stale_queued(now - RECONCILER_QUEUED_GRACE, limit)

        republished = 0
        for run_id, attempt in running:
            async with self._uow_factory() as uow:
                if attempt < MAX_ATTEMPTS:
                    changed = await uow.runs.requeue(run_id, worker_id=None, available_at=now)
                else:
                    changed = await uow.runs.finish(
                        run_id,
                        from_state="running",
                        worker_id=None,
                        state="failed",
                        error_code="lease_expired",
                        error_message="lease expired after the last attempt",
                        now=now,
                    )
                message = await uow.runs.run_message(run_id) if changed else None
                await uow.commit()
            # T13 also republishes, so that RunGuard closes the check-run.
            if message is not None and await self._publish_run(message):
                republished += 1
        for pointer in publishing:
            try:
                await self._review_queue.publish_review(pointer)
            except Exception:
                _LOGGER.exception("T17 republication failed for run %s", pointer.run_id)
                continue
            republished += 1
        for message in queued:
            if await self._publish_run(message):
                republished += 1
        return republished

    async def _publish_run(self, message: PendingRunMessage) -> bool:
        try:
            await self._run_publisher.publish_confirmed(message)
        except Exception:
            _LOGGER.exception("Reconciler republication failed for run %s", message.run_id)
            return False
        return True
