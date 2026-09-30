"""Worker handling of one ``review.run/v1`` delivery: RunGuard, claim and attempt (T4-T11)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.check_runs import (
    CheckRunGateway,
    CheckRunReport,
    CheckRunTarget,
    check_run_view,
)
from app.modules.reviews.application.queue_messages import RunRetryQueue
from app.modules.reviews.application.run_failures import (
    FAST_ATTEMPT_DEADLINE,
    HEARTBEAT_INTERVAL,
    LEASE,
    MAX_ATTEMPTS,
    RETRYABLE_ERROR_CODES,
    RetryDelays,
    RunCancelled,
    RunFailure,
    classify_failure,
)
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage

_LOGGER = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "skipped"})
# §5.3: a Run does not start when the day's remainder is below the fast run limit.
FAST_RUN_COST_LIMIT = Decimal("0.50")


class DeliveryOutcome(StrEnum):
    ACK = "ack"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class RunGuardSnapshot:
    """PostgreSQL state RunGuard decides on, read under the Run row lock."""

    run_id: UUID
    workspace_id: UUID
    state: str
    attempt: int
    available_at: datetime
    cancel_requested: bool
    head_sha: str
    pr_head_sha: str
    pr_open: bool
    repository_enabled: bool
    daily_budget_usd: Decimal
    spent_today_usd: Decimal
    engine: str
    prompt_version_id: UUID
    rule_version_id: UUID
    check_run: CheckRunTarget


@dataclass(frozen=True)
class ClaimedAttempt:
    """What the pipeline and the LLM gateway (#33) know about the running attempt."""

    run_id: UUID
    workspace_id: UUID
    attempt: int
    engine: str
    deadline: datetime
    prompt_version_id: UUID
    rule_version_id: UUID
    worker_id: str


class CheckRunReports(Protocol):
    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None: ...


class CheckRunReportUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> CheckRunReports: ...


class RunLifecycleStore(CheckRunReports, Protocol):
    async def lock_for_claim(self, run_id: UUID) -> RunGuardSnapshot | None: ...

    async def claim(
        self, run_id: UUID, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> int: ...

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
    ) -> bool: ...

    async def extend_lease(self, run_id: UUID, worker_id: str, lease_until: datetime) -> bool: ...

    async def requeue(self, run_id: UUID, *, worker_id: str | None, available_at: datetime) -> bool:
        """``running`` → ``queued`` with ``lease_until = null`` (T9, T12)."""
        ...

    async def cancellation_reason(self, run_id: UUID) -> str | None: ...

    async def run_message(self, run_id: UUID) -> PendingRunMessage | None: ...


class RunLifecycleUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> RunLifecycleStore: ...


class RunSelectionRule(Protocol):
    """Repository PR selection rule (T5 ``rule_not_matched``)."""

    async def matches(self, run_id: UUID) -> bool: ...


class MatchAllRuns:
    """No selection rule exists in the schema yet: every PR passes."""

    async def matches(self, run_id: UUID) -> bool:
        return True


type ReviewPipeline = Callable[[ClaimedAttempt], Coroutine[Any, Any, bool]]


class _LeaseLost(Exception):
    pass


class _DeadlineReached(Exception):
    pass


class AttemptCheckpoint:
    """Checkpoint before each LLM call: attempt deadline and ``cancel_requested`` (§3, T11)."""

    def __init__(
        self,
        uow_factory: Callable[[], RunLifecycleUnitOfWork],
        run_id: UUID,
        deadline: datetime,
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._run_id = run_id
        self._deadline = deadline
        self._now = now

    async def __call__(self) -> None:
        if self._now() >= self._deadline:
            raise RunFailure("deadline_exceeded", "attempt deadline passed on a checkpoint")
        async with self._uow_factory() as uow:
            reason = await uow.runs.cancellation_reason(self._run_id)
        if reason is not None:
            raise RunCancelled(reason)


class HandleReviewRun:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], RunLifecycleUnitOfWork],
        pipeline: ReviewPipeline,
        retry_queue: RunRetryQueue,
        check_runs: CheckRunGateway | None,
        worker_id: str,
        selection: RunSelectionRule | None = None,
        delays: RetryDelays | None = None,
        attempt_deadline: timedelta = FAST_ATTEMPT_DEADLINE,
        heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
        lease: timedelta = LEASE,
        run_url: Callable[[UUID], str | None] = lambda _: None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._pipeline = pipeline
        self._retry_queue = retry_queue
        self._check_runs = check_runs
        self._worker_id = worker_id
        self._selection = selection or MatchAllRuns()
        self._delays = delays or RetryDelays()
        self._attempt_deadline = attempt_deadline
        self._heartbeat = heartbeat_interval.total_seconds()
        self._lease = lease
        self._run_url = run_url
        self._now = now

    async def execute(self, run_id: UUID) -> DeliveryOutcome:
        matches = await self._selection.matches(run_id)
        async with self._uow_factory() as uow:
            snapshot = await uow.runs.lock_for_claim(run_id)
            now = self._now()
            if snapshot is None:
                return DeliveryOutcome.ACK
            if snapshot.state in TERMINAL_STATES and snapshot.attempt >= 1:
                # RunGuard closes the check-run of a Run finished without a worker (T6, T13).
                decision: tuple[str, str] | None = (snapshot.state, "closed")
            elif snapshot.state != "queued" or snapshot.available_at > now:
                return DeliveryOutcome.ACK
            else:
                decision = self._guard(snapshot, matches)
                if decision is not None:
                    state, reason = decision
                    await uow.runs.finish(
                        run_id,
                        from_state="queued",
                        worker_id=None,
                        state=state,
                        error_code=reason,
                        error_message=None,
                        now=now,
                    )
                else:
                    attempt = await uow.runs.claim(
                        run_id, worker_id=self._worker_id, now=now, lease_until=now + self._lease
                    )
                await uow.commit()

        if decision is not None:
            state, reason = decision
            # No check-run before the first claim, except an immediate T5 skip (§7).
            if reason == "closed" or (
                reason != "repo_disabled" and (state == "skipped" or snapshot.attempt >= 1)
            ):
                await self._update_check_run(run_id)
            return DeliveryOutcome.ACK

        await self._update_check_run(run_id)
        return await self._run_attempt(
            ClaimedAttempt(
                run_id=run_id,
                workspace_id=snapshot.workspace_id,
                attempt=attempt,
                engine=snapshot.engine,
                deadline=now + self._attempt_deadline,
                prompt_version_id=snapshot.prompt_version_id,
                rule_version_id=snapshot.rule_version_id,
                worker_id=self._worker_id,
            )
        )

    @staticmethod
    def _guard(snapshot: RunGuardSnapshot, matches: bool) -> tuple[str, str] | None:
        if not snapshot.pr_open:
            return "cancelled", "pr_closed"
        if snapshot.head_sha != snapshot.pr_head_sha:
            return "cancelled", "superseded"
        if snapshot.cancel_requested:
            return "cancelled", "cancelled_by_user"
        if not snapshot.repository_enabled:
            return "skipped", "repo_disabled"
        if not matches:
            return "skipped", "rule_not_matched"
        # daily_budget_usd = 0 means that no daily limit is configured.
        if (
            snapshot.daily_budget_usd > 0
            and snapshot.daily_budget_usd - snapshot.spent_today_usd < FAST_RUN_COST_LIMIT
        ):
            return "skipped", "budget_paused"
        return None

    async def _run_attempt(self, claimed: ClaimedAttempt) -> DeliveryOutcome:
        try:
            if await self._watch(claimed):
                return DeliveryOutcome.ACK
            reason = await self._cancellation_reason(claimed.run_id)
            if reason is not None:
                return await self._cancel(claimed, reason)
            failure = RunFailure("internal_error", "the review pipeline produced no result")
        except _LeaseLost:
            _LOGGER.warning("Run %s lease was taken over; the attempt stops", claimed.run_id)
            return DeliveryOutcome.ACK
        except RunCancelled as cancelled:
            reason = await self._cancellation_reason(claimed.run_id)
            return await self._cancel(claimed, reason or str(cancelled))
        except _DeadlineReached:
            failure = RunFailure("deadline_exceeded", "the attempt watchdog stopped the run")
        except Exception as exc:
            failure = classify_failure(exc)
        return await self._fail(claimed, failure)

    async def _watch(self, claimed: ClaimedAttempt) -> bool:
        """Run the pipeline under the attempt watchdog, renewing the lease until the deadline."""
        task = asyncio.ensure_future(self._pipeline(claimed))
        try:
            while True:
                remaining = (claimed.deadline - self._now()).total_seconds()
                if remaining <= 0:
                    raise _DeadlineReached
                done, _ = await asyncio.wait({task}, timeout=min(self._heartbeat, remaining))
                if task in done:
                    return task.result()
                now = self._now()
                if now >= claimed.deadline:
                    # Heartbeat is sent only before the attempt deadline (T7).
                    raise _DeadlineReached
                async with self._uow_factory() as uow:
                    extended = await uow.runs.extend_lease(
                        claimed.run_id, claimed.worker_id, now + self._lease
                    )
                    await uow.commit()
                if not extended:
                    raise _LeaseLost
        finally:
            if not task.done():
                task.cancel()
                # A phase that ignores cancellation is left to lease expiry (T12, T13).
                await asyncio.wait({task}, timeout=5)

    async def _cancellation_reason(self, run_id: UUID) -> str | None:
        async with self._uow_factory() as uow:
            return await uow.runs.cancellation_reason(run_id)

    async def _cancel(self, claimed: ClaimedAttempt, reason: str) -> DeliveryOutcome:
        if await self._finish_running(claimed, "cancelled", reason, None):
            await self._update_check_run(claimed.run_id)
        return DeliveryOutcome.ACK

    async def _fail(self, claimed: ClaimedAttempt, failure: RunFailure) -> DeliveryOutcome:
        retryable = failure.error_code in RETRYABLE_ERROR_CODES
        if retryable and claimed.attempt < MAX_ATTEMPTS:
            key, delay = self._delays.for_failure(claimed.attempt, failure)
            async with self._uow_factory() as uow:
                requeued = await uow.runs.requeue(
                    claimed.run_id,
                    worker_id=claimed.worker_id,
                    available_at=self._now() + delay,
                )
                message = await uow.runs.run_message(claimed.run_id)
                await uow.commit()
            if requeued and message is not None:
                _LOGGER.warning(
                    "Run %s attempt %s failed with %s (%s); retry in %s",
                    claimed.run_id,
                    claimed.attempt,
                    failure.error_code,
                    failure.message,
                    key,
                )
                # A failed confirm propagates: redelivery is acked by RunGuard, T18 republishes.
                await self._retry_queue.publish_retry(message, key)
            return DeliveryOutcome.ACK
        if not await self._finish_running(
            claimed, "failed", failure.error_code, failure.message[:2000]
        ):
            return DeliveryOutcome.ACK
        await self._update_check_run(claimed.run_id)
        return DeliveryOutcome.DEAD_LETTER if retryable else DeliveryOutcome.ACK

    async def _finish_running(
        self, claimed: ClaimedAttempt, state: str, error_code: str, error_message: str | None
    ) -> bool:
        async with self._uow_factory() as uow:
            finished = await uow.runs.finish(
                claimed.run_id,
                from_state="running",
                worker_id=claimed.worker_id,
                state=state,
                error_code=error_code,
                error_message=error_message,
                now=self._now(),
            )
            await uow.commit()
        return finished

    async def _update_check_run(self, run_id: UUID) -> None:
        await update_check_run(self._uow_factory, self._check_runs, run_id, self._run_url)


async def update_check_run(
    uow_factory: Callable[[], CheckRunReportUnitOfWork],
    gateway: CheckRunGateway | None,
    run_id: UUID,
    run_url: Callable[[UUID], str | None],
) -> None:
    """Show the current Run state on its check-run; a GitHub error never changes the Run (§5.2)."""
    if gateway is None:
        return
    async with uow_factory() as uow:
        report = await uow.runs.check_run_report(run_id)
    if report is None:
        return
    report = replace(report, run_url=run_url(run_id))
    try:
        await gateway.upsert(report.target, check_run_view(report))
    except Exception:
        _LOGGER.exception("Check-run update failed for run %s", run_id)
