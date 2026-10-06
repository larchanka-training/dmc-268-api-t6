"""Application service for an idempotent review-run cancellation request."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.run_events import RunUpdated, RunUpdatePublisher


@dataclass(frozen=True)
class CancelRequestResult:
    """Whether a run existed and cancellation durably changed it."""

    found: bool
    changed: bool
    signal_requested: bool = False


class CancellationSignals(Protocol):
    """Publish the T6 close signal after commit; a failure is left to the outbox replay."""

    async def publish_for(self, run_ids: tuple[UUID, ...]) -> int: ...


class CancelRunRepository(Protocol):
    async def request_cancel(self, run_id: UUID) -> CancelRequestResult: ...

    async def get_run(self, run_id: UUID) -> RunListItem | None: ...


class CancelRunUnitOfWork(UnitOfWork, Protocol):
    @property
    def repository(self) -> CancelRunRepository: ...


class CancelRun:
    def __init__(
        self,
        event_publisher: RunUpdatePublisher | None = None,
        signals: CancellationSignals | None = None,
        *,
        uow_factory: Callable[[], CancelRunUnitOfWork],
    ) -> None:
        self._event_publisher = event_publisher
        self._signals = signals
        self._uow_factory = uow_factory

    async def execute(self, run_id: UUID) -> RunListItem | None:
        async with self._uow_factory() as uow:
            cancellation = await uow.repository.request_cancel(run_id)
            if not cancellation.found:
                return None
            item = await uow.repository.get_run(run_id)
            await uow.commit()

        if cancellation.signal_requested and self._signals is not None:
            await self._signals.publish_for((run_id,))
        if cancellation.changed and item is not None and self._event_publisher is not None:
            await self._event_publisher.publish(RunUpdated(run_id=item.id, status=item.status))
        return item
