"""No-CI sweep application boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.modules.reviews.application.sweep_no_ci import DueNoCiCandidate, SweepNoCi
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
)

_CANDIDATE = DueNoCiCandidate(UUID("11111111-1111-1111-1111-111111111111"), "a" * 40)
_NOW = datetime(2026, 9, 30, tzinfo=UTC)


@dataclass
class Candidates:
    due: tuple[DueNoCiCandidate, ...] = (_CANDIDATE,)
    excluded: list[DueNoCiCandidate] = field(default_factory=list)

    async def list_due(self, now: datetime, limit: int) -> tuple[DueNoCiCandidate, ...]:
        assert now == _NOW
        assert limit == 100
        return self.due

    async def exclude(self, candidate: DueNoCiCandidate) -> None:
        self.excluded.append(candidate)


@dataclass
class Enqueuer:
    result: EnqueueResult
    calls: list[DueNoCiCandidate] = field(default_factory=list)

    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult:
        self.calls.append(DueNoCiCandidate(code_change_id, expected_head_sha))
        return self.result


def test_sweep_excludes_non_enqueued_candidate_until_state_changes() -> None:
    candidates = Candidates()
    enqueuer = Enqueuer(EnqueueResult(EnqueueStatus.INELIGIBLE))

    attempted = asyncio.run(
        SweepNoCi(candidates=candidates, enqueuer=enqueuer, now=lambda: _NOW).execute()
    )

    assert attempted == 1
    assert enqueuer.calls == [_CANDIDATE]
    assert candidates.excluded == [_CANDIDATE]


def test_sweep_does_not_exclude_enqueued_candidate() -> None:
    candidates = Candidates()
    enqueuer = Enqueuer(EnqueueResult(EnqueueStatus.ENQUEUED))

    asyncio.run(SweepNoCi(candidates=candidates, enqueuer=enqueuer, now=lambda: _NOW).execute())

    assert candidates.excluded == []
