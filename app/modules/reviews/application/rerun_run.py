"""``POST /api/runs/{id}/rerun``: a new Run for the PR's current head (PIPELINE_SPEC T3)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.get_run import RunDetailRepository
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
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class RerunResult:
    outcome: RerunOutcome
    message: PendingRunMessage | None = None


class RerunConflict(Exception):
    """The PR already has an active Run or is closed; no Run was created."""


class RerunNotConfigured(Exception):
    """The repository has no active rule or prompt version to review with."""


class RerunStore(Protocol):
    async def create_rerun(self, run_id: UUID, now: datetime) -> RerunResult:
        """Add the queued ``rerun`` Run and its ``NOTIFY``; flushes only."""
        ...

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None: ...


class RerunUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> RerunStore: ...


class RerunRun:
    def __init__(
        self,
        uow_factory: Callable[[], RerunUnitOfWork],
        runs: RunDetailRepository,
        publisher: RunMessagePublisher | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._runs = runs
        self._publisher = publisher
        self._now = now

    async def execute(self, run_id: UUID) -> RunListItem | None:
        async with self._uow_factory() as uow:
            result = await uow.runs.create_rerun(run_id, self._now())
            if result.outcome is RerunOutcome.CREATED:
                await uow.commit()
        if result.outcome is RerunOutcome.NOT_FOUND:
            return None
        if result.outcome is RerunOutcome.CONFLICT:
            raise RerunConflict
        if result.outcome is RerunOutcome.NOT_CONFIGURED:
            raise RerunNotConfigured
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
                async with self._uow_factory() as uow:
                    await uow.runs.mark_rerun_published(message.run_id, self._now())
                    await uow.commit()
        return await self._runs.get_run(message.run_id)
