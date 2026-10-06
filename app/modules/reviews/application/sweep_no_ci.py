"""Re-evaluate PRs whose automatic CI wait window has elapsed."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
    describe_enqueue,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DueNoCiCandidate:
    code_change_id: UUID
    head_sha: str


class DueNoCiCandidates(Protocol):
    """Short-transaction persistence boundary used by the worker leader in #34."""

    async def list_due(self, now: datetime, limit: int) -> tuple[DueNoCiCandidate, ...]: ...

    async def exclude(self, candidate: DueNoCiCandidate) -> None: ...


class SweepNoCiUnitOfWork(UnitOfWork, Protocol):
    @property
    def candidates(self) -> DueNoCiCandidates: ...


class WebhookRunEnqueuer(Protocol):
    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult: ...


class SweepNoCi:
    """Run the no-CI fallback outside the candidate-selection transaction."""

    def __init__(
        self,
        *,
        candidates: DueNoCiCandidates | None = None,
        enqueuer: WebhookRunEnqueuer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        uow_factory: Callable[[], SweepNoCiUnitOfWork] | None = None,
    ) -> None:
        if candidates is None and uow_factory is None:
            raise ValueError("Either candidates or uow_factory must be provided")
        self._candidates = candidates
        self._enqueuer = enqueuer
        self._now = now
        self._uow_factory = uow_factory

    async def execute(self, *, limit: int = 100) -> int:
        attempted = 0
        if self._uow_factory is not None:
            async with self._uow_factory() as uow:
                candidates_list = await uow.candidates.list_due(self._now(), limit)
        else:
            assert self._candidates is not None
            candidates_list = await self._candidates.list_due(self._now(), limit)

        for candidate in candidates_list:
            attempted += 1
            result = await self._enqueuer.execute(candidate.code_change_id, candidate.head_sha)
            excluded = result.status != EnqueueStatus.ENQUEUED
            if excluded:
                if self._uow_factory is not None:
                    async with self._uow_factory() as uow:
                        await uow.candidates.exclude(candidate)
                        await uow.commit()
                else:
                    assert self._candidates is not None
                    await self._candidates.exclude(candidate)
            _log_outcome(candidate, result, excluded=excluded)
        return attempted


def _log_outcome(candidate: DueNoCiCandidate, result: EnqueueResult, *, excluded: bool) -> None:
    """Log one line per candidate, after its exclusion (if any) is stored."""
    outcome = describe_enqueue(candidate.code_change_id, candidate.head_sha, result)
    if excluded:
        _LOGGER.info("No-CI sweep %s; excluded until the head or label changes", outcome)
    else:
        _LOGGER.info("No-CI sweep %s", outcome)
