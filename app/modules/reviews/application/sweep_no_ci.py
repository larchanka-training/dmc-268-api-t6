"""Re-evaluate PRs whose automatic CI wait window has elapsed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
)


@dataclass(frozen=True)
class DueNoCiCandidate:
    code_change_id: UUID
    head_sha: str


class DueNoCiCandidates(Protocol):
    """Short-transaction persistence boundary used by the worker leader in #34."""

    async def list_due(self, now: datetime, limit: int) -> tuple[DueNoCiCandidate, ...]: ...

    async def exclude(self, candidate: DueNoCiCandidate) -> None: ...


class WebhookRunEnqueuer(Protocol):
    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult: ...


class SweepNoCi:
    """Run the no-CI fallback outside the candidate-selection transaction."""

    def __init__(
        self,
        *,
        candidates: DueNoCiCandidates,
        enqueuer: WebhookRunEnqueuer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._candidates = candidates
        self._enqueuer = enqueuer
        self._now = now

    async def execute(self, *, limit: int = 100) -> int:
        attempted = 0
        for candidate in await self._candidates.list_due(self._now(), limit):
            attempted += 1
            result = await self._enqueuer.execute(candidate.code_change_id, candidate.head_sha)
            if result.status != EnqueueStatus.ENQUEUED:
                await self._candidates.exclude(candidate)
        return attempted
