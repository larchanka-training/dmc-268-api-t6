"""Application service for an idempotent review-run cancellation request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

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


class CancelRun:
    def __init__(
        self,
        repository: CancelRunRepository,
        event_publisher: RunUpdatePublisher | None = None,
        signals: CancellationSignals | None = None,
    ) -> None:
        self._repository = repository
        self._event_publisher = event_publisher
        self._signals = signals

    async def execute(self, run_id: UUID) -> RunListItem | None:
        cancellation = await self._repository.request_cancel(run_id)
        if not cancellation.found:
            return None
        if cancellation.signal_requested and self._signals is not None:
            await self._signals.publish_for((run_id,))
        item = await self._repository.get_run(run_id)
        if cancellation.changed and item is not None and self._event_publisher is not None:
            await self._event_publisher.publish(RunUpdated(run_id=item.id, status=item.status))
        return item
