"""Reconciler: T12, T13, T17, T18 (#34, docs/PIPELINE_SPEC.md §1)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

from app.modules.reviews.application.check_runs import CheckRunReport
from app.modules.reviews.application.handle_review_run import RunGuardSnapshot
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@dataclass
class Run:
    id: UUID
    state: str
    attempt: int
    lease_until: datetime | None = None
    available_at: datetime = NOW
    error_code: str | None = None


def message(run: Run) -> PendingRunMessage:
    return PendingRunMessage(
        run_id=run.id,
        workspace_id=UUID(int=1),
        installation_id=17,
        repository_id=UUID(int=2),
        repository_external_id=101,
        repository_full_name="octo/repo",
        pr_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        engine="fast",
        rule_version_id=UUID(int=3),
        prompt_version_id=UUID(int=4),
        attempt=run.attempt + 1,
        requested_at=NOW,
    )


@dataclass
class Store:
    runs: dict[UUID, Run]

    async def expired_running(self, now: datetime, limit: int) -> tuple[tuple[UUID, int], ...]:
        return tuple(
            (run.id, run.attempt)
            for run in self.runs.values()
            if run.state == "running" and run.lease_until is not None and run.lease_until < now
        )

    async def expired_publishing(
        self, now: datetime, limit: int
    ) -> tuple[ReviewPublishPointer, ...]:
        return tuple(
            ReviewPublishPointer(run.id, "a" * 40, "f" * 64, "COMMENT")
            for run in self.runs.values()
            if run.state == "publishing" and run.lease_until is not None and run.lease_until < now
        )

    async def stale_queued(
        self, available_before: datetime, limit: int
    ) -> tuple[PendingRunMessage, ...]:
        return tuple(
            message(run)
            for run in self.runs.values()
            if run.state == "queued" and run.available_at < available_before
        )

    async def requeue(self, run_id: UUID, *, worker_id: str | None, available_at: datetime) -> bool:
        run = self.runs[run_id]
        assert worker_id is None
        run.state, run.available_at, run.lease_until = "queued", available_at, None
        return True

    async def finish(
        self,
        run_id: UUID,
        *,
        from_state: str,
        worker_id: str | None,
        state: str,
        error_code: str | None,
        error_message: str | None,
        now: datetime,
    ) -> bool:
        run = self.runs[run_id]
        assert (from_state, worker_id) == ("running", None)
        run.state, run.error_code = state, error_code
        return True

    async def run_message(self, run_id: UUID) -> PendingRunMessage | None:
        return message(self.runs[run_id])

    async def lock_for_claim(self, run_id: UUID) -> RunGuardSnapshot | None:
        raise AssertionError("not used by the reconciler")

    async def claim(
        self, run_id: UUID, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> int:
        raise AssertionError("not used by the reconciler")

    async def extend_lease(self, run_id: UUID, worker_id: str, lease_until: datetime) -> bool:
        raise AssertionError("not used by the reconciler")

    async def cancellation_reason(self, run_id: UUID) -> str | None:
        raise AssertionError("not used by the reconciler")

    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
        raise AssertionError("not used by the reconciler")


class Uow:
    def __init__(self, store: Store) -> None:
        self.runs = store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class Publisher:
    runs: list[tuple[UUID, int, RunPublicationKind]] = field(default_factory=list)
    reviews: list[UUID] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        self.runs.append((message.run_id, message.attempt, kind))

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        self.reviews.append(pointer.run_id)


def test_reconciler_applies_t12_t13_t17_t18_and_leaves_retry_waits_alone() -> None:
    expired = NOW - timedelta(seconds=1)
    runs = {
        run.id: run
        for run in (
            Run(UUID(int=12), "running", 2, lease_until=expired),
            Run(UUID(int=13), "running", 3, lease_until=expired),
            Run(UUID(int=14), "running", 1, lease_until=NOW + timedelta(minutes=1)),
            Run(UUID(int=17), "publishing", 1, lease_until=expired),
            Run(UUID(int=18), "queued", 0, available_at=NOW - timedelta(minutes=11)),
            Run(UUID(int=19), "queued", 1, available_at=NOW + timedelta(minutes=2)),
            Run(UUID(int=20), "queued", 0, available_at=NOW - timedelta(minutes=9)),
        )
    }
    store = Store(runs)
    publisher = Publisher()

    republished = asyncio.run(
        ReconcileRuns(
            uow_factory=lambda: Uow(store),
            run_publisher=publisher,
            review_queue=publisher,
            now=lambda: NOW,
        ).execute()
    )

    # T12: back to queued, next attempt announced in the message.
    assert (runs[UUID(int=12)].state, runs[UUID(int=12)].available_at) == ("queued", NOW)
    # T13: failed with lease_expired and still published so RunGuard closes the check-run.
    assert (runs[UUID(int=13)].state, runs[UUID(int=13)].error_code) == (
        "failed",
        "lease_expired",
    )
    assert runs[UUID(int=14)].state == "running"
    assert publisher.runs == [
        (UUID(int=12), 3, RunPublicationKind.QUEUED),
        (UUID(int=13), 4, RunPublicationKind.QUEUED),
        (UUID(int=18), 1, RunPublicationKind.QUEUED),
    ]
    # T17: publishing republishes review.publish/v1 without a state change.
    assert publisher.reviews == [UUID(int=17)]
    assert runs[UUID(int=17)].state == "publishing"
    # A Run waiting in a retry queue (available_at in the future) is not touched.
    assert runs[UUID(int=19)].state == "queued"
    assert republished == 4
