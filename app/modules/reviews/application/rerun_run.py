"""``POST /api/runs/{id}/rerun``: a new Run for the PR's current head (PIPELINE_SPEC T3)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunMessagePublisher,
)

_LOGGER = logging.getLogger(__name__)


class RerunOutcome(StrEnum):
    CREATED = "created"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class RerunResult:
    outcome: RerunOutcome
    message: PendingRunMessage | None = None


class RerunConflict(Exception):
    """The PR already has an active Run or is closed; no Run was created."""


class RerunRepository(Protocol):
    async def create_rerun(self, run_id: UUID, now: datetime) -> RerunResult:
        """Insert the queued ``rerun`` Run and ``NOTIFY`` in one transaction."""
        ...

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None: ...

    async def get_run(self, run_id: UUID) -> RunListItem | None: ...


class RerunRun:
    def __init__(
        self,
        repository: RerunRepository,
        publisher: RunMessagePublisher | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._now = now

    async def execute(self, run_id: UUID) -> RunListItem | None:
        result = await self._repository.create_rerun(run_id, self._now())
        if result.outcome is RerunOutcome.NOT_FOUND:
            return None
        if result.outcome is RerunOutcome.CONFLICT:
            raise RerunConflict
        assert result.message is not None
        message = result.message
        if self._publisher is not None:
            try:
                # AMQP priority 9 comes from the ``rerun`` trigger of the message.
                await self._publisher.publish_confirmed(message)
            except Exception:
                # The reconciler republishes a queued Run after 10 minutes (T18).
                _LOGGER.exception("Rerun %s publication remains pending", message.run_id)
            else:
                await self._repository.mark_rerun_published(message.run_id, self._now())
        return await self._repository.get_run(message.run_id)
