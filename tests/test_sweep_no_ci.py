"""No-CI sweep application boundary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.modules.reviews.application.sweep_no_ci import DueNoCiCandidate, SweepNoCi
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
)
from tests.trigger_uow import candidates_uow

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
        SweepNoCi(
            uow_factory=candidates_uow(candidates), enqueuer=enqueuer, now=lambda: _NOW
        ).execute()
    )

    assert attempted == 1
    assert enqueuer.calls == [_CANDIDATE]
    assert candidates.excluded == [_CANDIDATE]


def test_sweep_does_not_exclude_enqueued_candidate() -> None:
    candidates = Candidates()
    enqueuer = Enqueuer(EnqueueResult(EnqueueStatus.ENQUEUED))

    asyncio.run(
        SweepNoCi(
            uow_factory=candidates_uow(candidates), enqueuer=enqueuer, now=lambda: _NOW
        ).execute()
    )

    assert candidates.excluded == []


_SWEEP_LOGGER = "app.modules.reviews.application.sweep_no_ci"


def _sweep_lines(result: EnqueueResult, caplog: pytest.LogCaptureFixture) -> list[str]:
    sweep = SweepNoCi(
        uow_factory=candidates_uow(Candidates()), enqueuer=Enqueuer(result), now=lambda: _NOW
    )
    with caplog.at_level(logging.INFO, logger=_SWEEP_LOGGER):
        asyncio.run(sweep.execute())
    return [
        f"{record.levelname} {record.getMessage()}"
        for record in caplog.records
        if record.name == _SWEEP_LOGGER
    ]


def test_sweep_logs_the_outcome_of_an_enqueued_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_id = UUID("22222222-2222-2222-2222-222222222222")

    lines = _sweep_lines(EnqueueResult(EnqueueStatus.ENQUEUED, run_id), caplog)

    assert lines == [
        "INFO No-CI sweep pr=11111111-1111-1111-1111-111111111111 head=aaaaaaa: enqueued "
        "run=22222222-2222-2222-2222-222222222222"
    ]


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (
            EnqueueResult(EnqueueStatus.UNCONFIGURED, reason="missing_rules"),
            "unconfigured (missing_rules)",
        ),
        (
            EnqueueResult(
                EnqueueStatus.INELIGIBLE, reason="ci_blocked", detail="commit status failure"
            ),
            "ineligible (ci_blocked: commit status failure)",
        ),
    ],
)
def test_sweep_logs_the_outcome_of_an_excluded_candidate(
    result: EnqueueResult, outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    lines = _sweep_lines(result, caplog)

    assert lines == [
        "INFO No-CI sweep pr=11111111-1111-1111-1111-111111111111 head=aaaaaaa: "
        f"{outcome}; excluded until the head or label changes"
    ]


@dataclass
class FakeUnitOfWork:
    candidates: Candidates
    committed: bool = False

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        pass


def test_sweep_with_unit_of_work_commits_on_exclusion() -> None:
    candidates = Candidates()
    uow = FakeUnitOfWork(candidates)
    enqueuer = Enqueuer(EnqueueResult(EnqueueStatus.INELIGIBLE))

    attempted = asyncio.run(
        SweepNoCi(uow_factory=lambda: uow, enqueuer=enqueuer, now=lambda: _NOW).execute()
    )

    assert attempted == 1
    assert enqueuer.calls == [_CANDIDATE]
    assert candidates.excluded == [_CANDIDATE]
    assert uow.committed is True
