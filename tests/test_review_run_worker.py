"""Worker handling of review.run/v1: RunGuard, claim, attempt, retry and DLQ (#34)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    CheckRunView,
    check_run_view,
)
from app.modules.reviews.application.handle_review_run import (
    AttemptCheckpoint,
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
    RunGuardSnapshot,
)
from app.modules.reviews.application.review_output import InvalidReviewOutput
from app.modules.reviews.application.run_failures import RetryDelays, RunCancelled, RunFailure
from app.modules.reviews.application.run_trace import traced_step
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
            check_run=CheckRunTarget(17, "octo/repo", run.head_sha, run_id),
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

    async def upsert(self, target: CheckRunTarget, view: CheckRunView) -> None:
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


def never_called(claimed: ClaimedAttempt) -> bool:
    raise AssertionError("the pipeline and ReviewModel must not run")


@pytest.mark.parametrize(
    ("change", "selection", "reason", "creates_check_run"),
    [
        ({"repository_enabled": False}, True, "repo_disabled", False),
        ({}, False, "rule_not_matched", True),
        (
            {"daily_budget_usd": Decimal("10"), "spent_today_usd": Decimal("9.60")},
            True,
            "budget_paused",
            True,
        ),
    ],
)
def test_run_guard_skips_without_running_the_pipeline(
    change: dict[str, object], selection: bool, reason: str, creates_check_run: bool
) -> None:
    run = FakeRun(**change)  # type: ignore[arg-type]
    check_runs = CheckRuns()
    outcome = asyncio.run(
        handler(run, never_called, check_runs=check_runs, selection=Selection(selection)).execute(
            RUN
        )
    )

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code, run.attempt) == ("skipped", reason, 0)
    assert run.notifications == ["skipped"]
    if creates_check_run:
        assert [(v.status, v.conclusion) for v in check_runs.views] == [("completed", "skipped")]
    else:
        assert check_runs.views == []


def test_zero_daily_budget_means_no_limit() -> None:
    run = FakeRun(daily_budget_usd=Decimal("0"), spent_today_usd=Decimal("100"))
    outcome = asyncio.run(handler(run, lambda claimed: True).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.attempt) == ("running", 1)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"pr_head_sha": "c" * 40}, "superseded"),
        ({"pr_open": False}, "pr_closed"),
        ({"cancel_requested": True}, "cancelled_by_user"),
    ],
)
@pytest.mark.parametrize("attempt", [0, 1])
def test_run_guard_cancels_stale_closed_or_cancelled_runs(
    change: dict[str, object], reason: str, attempt: int
) -> None:
    run = FakeRun(attempt=attempt, **change)  # type: ignore[arg-type]
    check_runs = CheckRuns()
    outcome = asyncio.run(handler(run, never_called, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("cancelled", reason)
    # Before the first claim there is no check-run (§7); afterwards RunGuard closes it.
    expected = [("completed", "cancelled")] if attempt >= 1 else []
    assert [(v.status, v.conclusion) for v in check_runs.views] == expected


@pytest.mark.parametrize(
    ("state", "error_code", "conclusion"),
    [("cancelled", "superseded", "cancelled"), ("failed", "lease_expired", "neutral")],
)
def test_run_guard_closes_the_check_run_of_a_run_finished_without_a_worker(
    state: str, error_code: str, conclusion: str
) -> None:
    run = FakeRun(
        state=state,
        attempt=1,
        error_code=error_code,
        available_at=NOW + timedelta(minutes=2),
    )
    check_runs = CheckRuns()
    outcome = asyncio.run(handler(run, never_called, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert [(v.status, v.conclusion) for v in check_runs.views] == [("completed", conclusion)]
    assert run.notifications == []


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(state="running", attempt=1),
        FakeRun(state="publishing", attempt=1),
        FakeRun(available_at=NOW + timedelta(seconds=30), attempt=1),
    ],
)
def test_redelivery_for_a_run_not_claimable_is_acked_without_work(run: FakeRun) -> None:
    check_runs = CheckRuns()
    before = (run.state, run.attempt)
    outcome = asyncio.run(handler(run, never_called, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.attempt) == before
    assert check_runs.views == []


def test_claim_takes_the_lease_and_passes_the_attempt_deadline() -> None:
    run = FakeRun()
    check_runs = CheckRuns()
    seen: list[ClaimedAttempt] = []

    def pipeline(claimed: ClaimedAttempt) -> bool:
        seen.append(claimed)
        assert (run.state, run.worker_id, run.lease_until) == (
            "running",
            "worker-1",
            NOW + timedelta(minutes=5),
        )
        return True

    outcome = asyncio.run(handler(run, pipeline, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert run.attempt == 1
    assert seen[0].deadline == NOW + timedelta(minutes=8)
    assert seen[0].attempt == 1
    assert check_runs.views == [
        CheckRunView("in_progress", None, "AI-ревью выполняется", "попытка 1 из 3")
    ]


def test_retryable_failures_retry_twice_then_fail_into_the_dead_letter_queue() -> None:
    run = FakeRun()
    retry = RetryQueue()
    check_runs = CheckRuns()

    def failing(claimed: ClaimedAttempt) -> bool:
        raise RunFailure("llm_unavailable", "provider down")

    outcomes = []
    for delay in (timedelta(0), timedelta(seconds=30), timedelta(minutes=2)):
        clock = Clock(NOW + delay)
        run.available_at = min(run.available_at, clock.value)
        outcomes.append(
            asyncio.run(
                handler(run, failing, retry=retry, check_runs=check_runs, clock=clock).execute(RUN)
            )
        )

    assert outcomes == [DeliveryOutcome.ACK, DeliveryOutcome.ACK, DeliveryOutcome.DEAD_LETTER]
    assert retry.published == [("30s", 2), ("2m", 3)]
    assert (run.state, run.error_code, run.attempt) == ("failed", "llm_unavailable", 3)
    assert run.notifications == ["running", "queued", "running", "queued", "running", "failed"]
    assert [v.summary for v in check_runs.views[:3]] == [
        "попытка 1 из 3",
        "попытка 2 из 3",
        "попытка 3 из 3",
    ]
    assert (check_runs.views[-1].status, check_runs.views[-1].conclusion) == (
        "completed",
        "neutral",
    )


def test_first_retry_sets_available_at_to_the_retry_delay() -> None:
    run = FakeRun()

    def failing(claimed: ClaimedAttempt) -> bool:
        raise RunFailure("diff_fetch_failed", "GitHub answered 502")

    asyncio.run(handler(run, failing).execute(RUN))

    assert (run.state, run.available_at, run.lease_until) == (
        "queued",
        NOW + timedelta(seconds=30),
        None,
    )


def test_rate_limit_with_long_retry_after_uses_the_ten_minute_queue() -> None:
    failure = RunFailure("llm_rate_limited", retry_after=timedelta(minutes=5))
    assert RetryDelays().for_failure(1, failure) == ("10m", timedelta(minutes=10))
    assert RetryDelays().for_failure(1, RunFailure("llm_rate_limited")) == (
        "2m",
        timedelta(minutes=2),
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (InvalidReviewOutput("bad json"), "llm_invalid_output"),
        (RunFailure("github_forbidden"), "github_forbidden"),
    ],
)
def test_failures_without_retry_fail_at_once_and_are_acked(error: Exception, code: str) -> None:
    run = FakeRun()
    retry = RetryQueue()

    def failing(claimed: ClaimedAttempt) -> bool:
        raise error

    outcome = asyncio.run(handler(run, failing, retry=retry).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("failed", code)
    assert retry.published == []


class GatewayDeadline(Exception):
    error_code = "deadline_exceeded"


def test_gateway_deadline_exceeded_fails_without_retry() -> None:
    run = FakeRun()

    def failing(claimed: ClaimedAttempt) -> bool:
        raise GatewayDeadline("gateway refused the call")

    outcome = asyncio.run(handler(run, failing).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("failed", "deadline_exceeded")


def test_checkpoint_after_the_deadline_fails_the_run_before_the_model_call() -> None:
    run = FakeRun()
    store = FakeStore(run)
    clock = Clock()

    async def pipeline(claimed: ClaimedAttempt) -> bool:
        clock.value = claimed.deadline + timedelta(seconds=1)
        await AttemptCheckpoint(lambda: FakeUow(store), RUN, claimed.deadline, clock)()
        raise AssertionError("the model call must not happen after the deadline")

    outcome = asyncio.run(handler(run, pipeline, clock=clock).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("failed", "deadline_exceeded")


def test_cancel_requested_before_the_model_call_cancels_the_run() -> None:
    run = FakeRun()
    store = FakeStore(run)
    check_runs = CheckRuns()

    async def pipeline(claimed: ClaimedAttempt) -> bool:
        run.cancel_requested = True
        await AttemptCheckpoint(lambda: FakeUow(store), RUN, claimed.deadline, lambda: NOW)()
        raise AssertionError("ReviewModel must not be called")

    outcome = asyncio.run(handler(run, pipeline, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("cancelled", "cancelled_by_user")
    assert (check_runs.views[-1].status, check_runs.views[-1].conclusion) == (
        "completed",
        "cancelled",
    )


def test_run_cancelled_uses_the_reason_from_postgresql() -> None:
    run = FakeRun()

    def pipeline(claimed: ClaimedAttempt) -> bool:
        run.pr_head_sha = "c" * 40
        raise RunCancelled("checkpoint")

    asyncio.run(handler(run, pipeline).execute(RUN))

    assert (run.state, run.error_code) == ("cancelled", "superseded")


def test_watchdog_stops_a_hanging_phase_and_heartbeat_stops_at_the_deadline() -> None:
    run = FakeRun()
    # Every clock read moves 90 s: several heartbeats fit before the 8-minute deadline.
    clock = Clock(step=timedelta(seconds=90))
    cancelled = asyncio.Event()

    async def hanging(claimed: ClaimedAttempt) -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return True

    outcome = asyncio.run(
        handler(run, hanging, clock=clock, heartbeat=timedelta(milliseconds=5)).execute(RUN)
    )

    deadline = NOW + timedelta(minutes=8)
    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("failed", "deadline_exceeded")
    assert cancelled.is_set()
    assert run.lease_extensions
    # Each renewal is now + 5 min for a "now" before the deadline.
    assert all(lease < deadline + timedelta(minutes=5) for lease in run.lease_extensions)


def test_lost_lease_stops_the_attempt_without_writes() -> None:
    run = FakeRun()
    run.lease_owner_lost = True

    async def hanging(claimed: ClaimedAttempt) -> bool:
        await asyncio.Event().wait()
        return True

    outcome = asyncio.run(handler(run, hanging, heartbeat=timedelta(milliseconds=5)).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("running", None)
    assert run.notifications == ["running"]


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (
            CheckRunReport(
                CheckRunTarget(1, "o/r", HEAD, RUN),
                "succeeded",
                1,
                verdict="blocking",
                severity_counts={"critical": 1},
                inline_count=1,
            ),
            ("completed", "neutral", "AI-ревью: blocking"),
        ),
        (
            CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "failed", 3, "llm_timeout"),
            ("completed", "neutral", "AI-ревью не выполнено"),
        ),
        (
            CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "cancelled", 1, "superseded"),
            ("completed", "cancelled", "AI-ревью отменено"),
        ),
        (
            CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "skipped", 0, "budget_paused"),
            ("completed", "skipped", "AI-ревью пропущено"),
        ),
        (
            CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "running", 2),
            ("in_progress", None, "AI-ревью выполняется"),
        ),
    ],
)
def test_check_run_view_follows_the_pipeline_spec_table(
    report: CheckRunReport, expected: tuple[str, str | None, str]
) -> None:
    view = check_run_view(report)
    assert (view.status, view.conclusion, view.title) == expected
    assert view.conclusion != "failure"


def test_failed_step_records_its_error_and_duration() -> None:
    records: list[tuple[str, object, int]] = []

    class Trace:
        async def record(
            self,
            run_id: UUID,
            tool: str,
            request: dict[str, object],
            response: object,
            started_at: datetime,
            duration_ms: int,
        ) -> None:
            records.append((tool, response, duration_ms))

    async def failing_step() -> None:
        async with traced_step(Trace(), RUN, "vcs.fetch_diff", {"pr_number": 7}):
            raise RunFailure("diff_fetch_failed", "GitHub answered 502")

    with pytest.raises(RunFailure):
        asyncio.run(failing_step())

    assert records == [
        (
            "vcs.fetch_diff",
            {"error": {"type": "RunFailure", "message": "diff_fetch_failed: GitHub answered 502"}},
            records[0][2],
        )
    ]
    assert records[0][2] >= 0
