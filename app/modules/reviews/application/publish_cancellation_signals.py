"""Publish durable pointers for attempted Runs cancelled by PR changes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunMessagePublisher,
    RunPublicationKind,
)

_LOGGER = logging.getLogger(__name__)


class CancellationSignalStore(Protocol):
    async def pending_cancellation_signals(
        self, limit: int, run_ids: tuple[UUID, ...] | None = None
    ) -> tuple[PendingRunMessage, ...]: ...

    async def mark_cancellation_signal_published(self, run_id: UUID, now: datetime) -> None: ...


class CancellationSignalUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> CancellationSignalStore: ...


class PublishCancellationSignals:
    """Confirm broker delivery after the cancellation UOW commits, with replay."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], CancellationSignalUnitOfWork],
        publisher: RunMessagePublisher,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._publisher = publisher
        self._now = now

    async def publish_for(self, run_ids: tuple[UUID, ...]) -> int:
        if not run_ids:
            return 0
        return await self._publish(limit=len(run_ids), run_ids=run_ids)

    async def replay_pending(self, *, limit: int = 100) -> int:
        return await self._publish(limit=limit)

    async def _publish(self, *, limit: int, run_ids: tuple[UUID, ...] | None = None) -> int:
        async with self._uow_factory() as uow:
            messages = await uow.runs.pending_cancellation_signals(limit, run_ids)
        confirmed = 0
        for message in messages:
            try:
                await self._publisher.publish_confirmed(
                    message, kind=RunPublicationKind.CANCELLATION
                )
            except Exception:
                _LOGGER.exception("Cancellation signal remains pending for run %s", message.run_id)
                continue
            async with self._uow_factory() as uow:
                await uow.runs.mark_cancellation_signal_published(message.run_id, self._now())
                await uow.commit()
            confirmed += 1
        return confirmed
