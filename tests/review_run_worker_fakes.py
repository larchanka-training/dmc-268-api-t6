"""Shared fakes of the review.run worker tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self
from uuid import UUID

from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    HandleReviewRun,
    RunGuardSnapshot,
)
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage

RUN = UUID("11111111-1111-1111-1111-111111111111")
WORKSPACE = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
HEAD = "a" * 40


@dataclass
class FakeRun:
    state: str = "queued"
    attempt: int = 0
    available_at: datetime = NOW
    cancel_requested: bool = False
    head_sha: str = HEAD
    pr_head_sha: str = HEAD
    pr_open: bool = True
    repository_enabled: bool = True
    daily_budget_usd: Decimal = Decimal("0")
    spent_today_usd: Decimal = Decimal("0")
    worker_id: str | None = None
    lease_until: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    notifications: list[str] = field(default_factory=list)
    lease_extensions: list[datetime] = field(default_factory=list)
    lease_owner_lost: bool = False


class FakeStore:
    def __init__(self, run: FakeRun) -> None:
        self.run = run

    async def lock_for_claim(self, run_id: UUID) -> RunGuardSnapshot | None:
        run = self.run
        return RunGuardSnapshot(
            run_id=run_id,
            workspace_id=WORKSPACE,
            state=run.state,
            attempt=run.attempt,
            available_at=run.available_at,
            cancel_requested=run.cancel_requested,
            head_sha=run.head_sha,
            pr_head_sha=run.pr_head_sha,
            pr_open=run.pr_open,
            repository_enabled=run.repository_enabled,
            daily_budget_usd=run.daily_budget_usd,
            spent_today_usd=run.spent_today_usd,
            engine="fast",
            prompt_version_id=UUID(int=3),
            rule_version_id=UUID(int=4),
        )

    async def claim(
        self, run_id: UUID, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> int:
        assert self.run.state == "queued"
        self.run.state = "running"
        self.run.attempt += 1
        self.run.worker_id = worker_id
        self.run.lease_until = lease_until
        self.run.notifications.append("running")
        return self.run.attempt

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
        if self.run.state != from_state or (
            worker_id is not None and self.run.worker_id != worker_id
        ):
            return False
        self.run.state = state
        self.run.error_code = error_code
        self.run.error_message = error_message
        self.run.lease_until = None
        self.run.notifications.append(state)
        return True

    async def extend_lease(self, run_id: UUID, worker_id: str, lease_until: datetime) -> bool:
        if self.run.lease_owner_lost:
            return False
        self.run.lease_extensions.append(lease_until)
        self.run.lease_until = lease_until
        return True

    async def requeue(self, run_id: UUID, *, worker_id: str | None, available_at: datetime) -> bool:
        if self.run.state != "running":
            return False
        self.run.state = "queued"
        self.run.available_at = available_at
        self.run.lease_until = None
        self.run.notifications.append("queued")
        return True

    async def cancellation_reason(self, run_id: UUID) -> str | None:
        if not self.run.pr_open:
            return "pr_closed"
        if self.run.head_sha != self.run.pr_head_sha:
            return "superseded"
        return "cancelled_by_user" if self.run.cancel_requested else None

    async def run_message(self, run_id: UUID) -> PendingRunMessage | None:
        return PendingRunMessage(
            run_id=run_id,
            workspace_id=WORKSPACE,
            installation_id=17,
            repository_id=UUID(int=5),
            repository_external_id=101,
            repository_full_name="octo/repo",
            pr_number=7,
            head_sha=self.run.head_sha,
            base_sha="b" * 40,
            base_ref="main",
            engine="fast",
            rule_version_id=UUID(int=4),
            prompt_version_id=UUID(int=3),
            attempt=self.run.attempt + 1,
            requested_at=NOW,
        )

    async def provider_origin_active(self, run_id: UUID) -> bool:
        return True

    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
        return CheckRunReport(
            CheckRunTarget(17, "octo/repo", self.run.head_sha, run_id),
            self.run.state,
            self.run.attempt,
            self.run.error_code,
        )


class FakeUow:
    def __init__(self, store: FakeStore) -> None:
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
class CheckRuns:
    views: list[CheckRunView] = field(default_factory=list)

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        await checkpoint()
        assert target.run_id == RUN
        self.views.append(view)


@dataclass
class RetryQueue:
    published: list[tuple[str, int]] = field(default_factory=list)

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        self.published.append((delay_key, message.attempt))


@dataclass
class Selection:
    result: bool = True

    async def matches(self, run_id: UUID) -> bool:
        return self.result


class Clock:
    def __init__(self, start: datetime = NOW, step: timedelta = timedelta(0)) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> datetime:
        current = self.value
        self.value += self.step
        return current


def handler(
    run: FakeRun,
    pipeline: Callable[[ClaimedAttempt], object],
    *,
    check_runs: CheckRuns | None = None,
    retry: RetryQueue | None = None,
    selection: Selection | None = None,
    clock: Callable[[], datetime] | None = None,
    heartbeat: timedelta = timedelta(seconds=60),
) -> HandleReviewRun:
    store = FakeStore(run)

    async def run_pipeline(claimed: ClaimedAttempt) -> bool:
        result = pipeline(claimed)
        if asyncio.iscoroutine(result):
            result = await result
        assert isinstance(result, bool)
        return result

    return HandleReviewRun(
        uow_factory=lambda: FakeUow(store),
        pipeline=run_pipeline,
        retry_queue=retry or RetryQueue(),
        check_runs=check_runs,
        worker_id="worker-1",
        selection=selection,
        heartbeat_interval=heartbeat,
        now=clock or (lambda: NOW),
    )
