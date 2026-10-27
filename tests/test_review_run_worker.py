"""Worker handling of review.run/v1: RunGuard, claim, attempt, retry and DLQ (#34)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self, cast
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application import review_ignore
from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
    check_run_view,
)
from app.modules.reviews.application.conventions import ConventionsRequest
from app.modules.reviews.application.handle_review_run import (
    AttemptCheckpoint,
    AttemptProvider,
    CheckpointedProvider,
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
    RunGuardSnapshot,
    update_check_run,
)
from app.modules.reviews.application.llm import LlmCallFailed, LlmErrorCode
from app.modules.reviews.application.policy_errors import (
    PolicyError,
    PolicyErrorCode,
    policy_error_detail,
)
from app.modules.reviews.application.prompt_builder import ReviewContext
from app.modules.reviews.application.review_ignore import IgnoreMatcher, parse_review_ignore
from app.modules.reviews.application.review_output import InvalidReviewOutput
from app.modules.reviews.application.run_failures import (
    RETRYABLE_ERROR_CODES,
    RetryDelays,
    RunCancelled,
    RunFailure,
    RunSkipped,
)
from app.modules.reviews.application.run_trace import traced_step
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.github_policy_source import classify_policy_http_error
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.vcs_failures import (
    ClassifiedVcsProvider,
    classify_vcs_error,
)

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
    "limited",
    [
        pytest.param(
            lambda delay: PolicyError(
                PolicyErrorCode.RATE_LIMITED, "GitHub answered 429", retry_after=delay
            ),
            id="policy",
        ),
        pytest.param(
            lambda delay: RunFailure("diff_fetch_failed", "GitHub answered 429", retry_after=delay),
            id="vcs",
        ),
        # The real gateway failure through the worker:
        # test_llm_rate_limit_retry_waits_out_the_provider_retry_after.
        pytest.param(lambda delay: RunFailure("llm_rate_limited", retry_after=delay), id="llm"),
    ],
)
@pytest.mark.parametrize(
    ("retry_after", "delay"),
    [
        pytest.param(timedelta(minutes=5), timedelta(minutes=10), id="5m"),
        pytest.param(timedelta(minutes=10), timedelta(minutes=10), id="10m"),
        pytest.param(timedelta(minutes=15), timedelta(minutes=15), id="15m"),
        pytest.param(timedelta(days=1), timedelta(days=1), id="1d"),
        pytest.param(timedelta(days=2), timedelta(days=1), id="2d-ceiling"),
    ],
)
def test_rate_limited_retry_never_comes_before_retry_after(
    limited: Callable[[timedelta], RunFailure], retry_after: timedelta, delay: timedelta
) -> None:
    # D3: the 10m queue stays; a longer wait lives in available_at, capped at one day.
    assert RetryDelays().for_failure(1, limited(retry_after)) == ("10m", delay)


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


def test_payment_required_fails_run_after_one_attempt_without_retry() -> None:
    run = FakeRun()
    retry = RetryQueue()
    check_runs = CheckRuns()

    def failing(claimed: ClaimedAttempt) -> bool:
        raise LlmCallFailed(LlmErrorCode.PAYMENT_REQUIRED, "HTTP 402: account has no credits")

    outcome = asyncio.run(handler(run, failing, retry=retry, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code, run.attempt) == ("failed", "llm_payment_required", 1)
    assert retry.published == []
    assert (check_runs.views[-1].status, check_runs.views[-1].conclusion) == (
        "completed",
        "neutral",
    )
    assert check_runs.views[-1].summary == "AI-ревью временно недоступно."


def test_payment_required_check_run_keeps_neutral_text_and_run_link() -> None:
    run_url = f"https://review.example/runs/{RUN}"
    report = CheckRunReport(
        CheckRunTarget(1, "o/r", HEAD, RUN),
        "failed",
        1,
        "llm_payment_required",
        run_url=run_url,
    )

    view = check_run_view(report)

    assert (view.status, view.conclusion, view.title) == (
        "completed",
        "neutral",
        "AI-ревью не выполнено",
    )
    assert view.summary == f"AI-ревью временно недоступно.\n\n{run_url}"
    assert "llm_payment_required" not in view.summary
    assert "402" not in view.summary


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


@pytest.mark.parametrize(
    ("run_url", "details_url"),
    [("https://app.test/runs/1", "https://app.test/runs/1"), (None, None)],
    ids=["run-link", "no-portal-url"],
)
@pytest.mark.parametrize(
    "report",
    [
        CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "running", 2),
        CheckRunReport(
            CheckRunTarget(1, "o/r", HEAD, RUN),
            "succeeded",
            1,
            verdict="blocking",
            severity_counts={"critical": 1},
            inline_count=1,
        ),
        CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "succeeded", 1, verdict=None),
        CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "succeeded", 1, summary_only=True),
        CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "failed", 3, "llm_timeout"),
        CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "failed", 1, "llm_payment_required"),
        CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "cancelled", 1, "superseded"),
        CheckRunReport(CheckRunTarget(1, "o/r", HEAD, RUN), "skipped", 0, "budget_paused"),
    ],
    ids=[
        "in-progress",
        "succeeded",
        "partial",
        "summary-only",
        "failed",
        "payment-required",
        "cancelled",
        "skipped",
    ],
)
def test_check_run_view_sends_the_run_link_as_details_url_in_every_state(
    report: CheckRunReport, run_url: str | None, details_url: str | None
) -> None:
    view = check_run_view(replace(report, run_url=run_url))
    assert view.details_url == details_url


@pytest.mark.parametrize(
    ("below_threshold", "expected"),
    [
        (
            0,
            "critical: 0, high: 1, medium: 0, low: 0, info: 0\n"
            "inline: 1, в теле ревью: 0\n\nhttps://app.test/runs/1",
        ),
        (
            3,
            "critical: 0, high: 1, medium: 0, low: 0, info: 0\n"
            "inline: 1, в теле ревью: 0\nниже порога severity: 3\n\nhttps://app.test/runs/1",
        ),
    ],
    ids=["none-below", "three-below"],
)
def test_succeeded_check_run_counts_findings_below_the_severity_threshold_only_when_present(
    below_threshold: int, expected: str
) -> None:
    view = check_run_view(
        CheckRunReport(
            CheckRunTarget(1, "o/r", HEAD, RUN),
            "succeeded",
            1,
            verdict="blocking",
            severity_counts={"high": 1},
            inline_count=1,
            below_threshold_count=below_threshold,
            run_url="https://app.test/runs/1",
        )
    )

    assert (view.status, view.conclusion, view.title) == (
        "completed",
        "neutral",
        "AI-ревью: blocking",
    )
    assert view.summary == expected


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


def test_checkpointed_provider_checks_before_every_llm_call_only() -> None:
    calls: list[str] = []

    class Inner:
        def __getattr__(self, name: str) -> object:
            async def call(*args: object, **kwargs: object) -> object:
                calls.append(name)
                return {}

            return call

    async def checkpoint() -> None:
        calls.append("checkpoint")
        if len(calls) > 3:
            raise RunCancelled("checkpoint")

    provider = CheckpointedProvider(cast(AttemptProvider, Inner()), checkpoint)

    async def scenario() -> None:
        await provider.fetch_tree(RUN)
        await provider.draft_conventions(request=cast(ConventionsRequest, None))
        with pytest.raises(RunCancelled):
            await provider.draft_review(context=cast(ReviewContext, None))

    asyncio.run(scenario())
    assert calls == ["fetch_tree", "checkpoint", "draft_conventions", "checkpoint"]


@pytest.mark.parametrize(
    ("status", "headers", "code", "retry_after"),
    [
        (403, {}, "github_forbidden", None),
        (404, {}, "github_forbidden", None),
        (403, {"X-RateLimit-Remaining": "0"}, "diff_fetch_failed", None),
        (429, {"Retry-After": "300"}, "diff_fetch_failed", timedelta(minutes=5)),
        (502, {}, "diff_fetch_failed", None),
    ],
)
def test_vcs_errors_map_to_their_error_code(
    status: int, headers: dict[str, str], code: str, retry_after: timedelta | None
) -> None:
    request = httpx.Request("GET", "https://api.github.test/repos/o/r/pulls/1")
    error = httpx.HTTPStatusError(
        "GitHub", request=request, response=httpx.Response(status, headers=headers, request=request)
    )
    failure = classify_vcs_error(error)
    assert (failure.error_code, failure.retry_after) == (code, retry_after)
    assert failure.error_code in RETRYABLE_ERROR_CODES or code == "github_forbidden"


def test_vcs_transport_error_is_a_retryable_diff_failure() -> None:
    failure = classify_vcs_error(httpx.ConnectTimeout("timed out"))
    assert failure.error_code == "diff_fetch_failed"


def test_skipped_attempt_finishes_the_run_as_skipped_with_its_reason() -> None:
    run = FakeRun()
    check_runs = CheckRuns()

    def pipeline(claimed: ClaimedAttempt) -> bool:
        raise RunSkipped("all_changes_ignored")

    outcome = asyncio.run(handler(run, pipeline, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code) == ("skipped", "all_changes_ignored")
    view = check_runs.views[-1]
    assert (view.status, view.conclusion, view.title) == (
        "completed",
        "skipped",
        "AI-ревью пропущено",
    )
    assert view.summary == "Все изменения исключены из AI-ревью."


def test_invalid_policy_fails_the_run_at_once_with_its_code() -> None:
    run = FakeRun()
    check_runs = CheckRuns()

    def pipeline(claimed: ClaimedAttempt) -> bool:
        raise PolicyError(
            PolicyErrorCode.DOCUMENT_TOO_LARGE, "The policy document exceeds 65536 bytes"
        )

    outcome = asyncio.run(handler(run, pipeline, check_runs=check_runs).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code, run.attempt) == ("failed", "policy_document_too_large", 1)
    assert "`policy_document_too_large`" in check_runs.views[-1].summary
    assert "64 КиБ" in check_runs.views[-1].summary


def test_exhausted_match_budget_fails_the_run_naming_the_document_and_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_ignore, "MAX_TRANSITIONS_PER_PATH", 20)
    matcher = IgnoreMatcher(parse_review_ignore("```review-ignore\n*a*a*a*b\n```\n"))
    run = FakeRun()

    def pipeline(claimed: ClaimedAttempt) -> bool:
        return matcher.match("a" * 40) is None

    outcome = asyncio.run(handler(run, pipeline).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.error_code, run.attempt) == (
        "failed",
        "policy_match_budget_exceeded",
        1,
    )
    # The Run keeps the code and the safe message with its location (RFS §9); the
    # stopped count is a lower bound, so the author sees an excess, not an exact value.
    assert policy_error_detail(run.error_message) == (
        "file `.review/rules.md`, actual more than 20, allowed 20 transitions"
    )


def test_policy_rate_limit_retry_waits_out_a_long_retry_after() -> None:
    run = FakeRun()
    retry = RetryQueue()
    clock = Clock()
    request = httpx.Request("GET", "https://api.github.test/repos/o/r/git/trees/" + "b" * 40)
    limited = httpx.HTTPStatusError(
        "GitHub",
        request=request,
        response=httpx.Response(429, headers={"Retry-After": "900"}, request=request),
    )

    def pipeline(claimed: ClaimedAttempt) -> bool:
        raise classify_policy_http_error(limited, policy_base_sha="b" * 40)

    first = asyncio.run(handler(run, pipeline, retry=retry, clock=clock).execute(RUN))

    assert first is DeliveryOutcome.ACK
    assert (run.state, run.attempt) == ("queued", 1)
    assert retry.published == [("10m", 2)]
    # Exactly Retry-After, not the ceiling and not the 10m bucket.
    assert run.available_at == NOW + timedelta(minutes=15)
    # The copy from the 10m queue comes early: RunGuard drops it without an attempt,
    # and T18 republishes the Run once available_at has passed (PIPELINE_SPEC §4.2).
    clock.value = NOW + timedelta(minutes=10)
    early = asyncio.run(handler(run, pipeline, retry=retry, clock=clock).execute(RUN))

    assert early is DeliveryOutcome.ACK
    assert (run.state, run.attempt, retry.published) == ("queued", 1, [("10m", 2)])


@pytest.mark.parametrize(
    ("retry_after_s", "key", "wait"),
    [
        pytest.param(900.0, "10m", timedelta(minutes=15), id="retry-after-15m"),
        pytest.param(None, "2m", timedelta(minutes=2), id="no-retry-after"),
    ],
)
def test_llm_rate_limit_retry_waits_out_the_provider_retry_after(
    retry_after_s: float | None,
    key: str,
    wait: timedelta,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = FakeRun()
    retry = RetryQueue()

    def pipeline(claimed: ClaimedAttempt) -> bool:
        # What the gateway raises for the review or the conventions call after its own
        # retry and fallback: nothing on the run path catches it before the worker.
        raise LlmCallFailed(LlmErrorCode.RATE_LIMITED, "HTTP 429", retry_after_s=retry_after_s)

    outcome = asyncio.run(handler(run, pipeline, retry=retry, clock=Clock()).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.attempt) == ("queued", 1)
    assert retry.published == [(key, 2)]
    assert run.available_at == NOW + wait
    # The log names the real wait, not only the queue, which caps at 10 minutes.
    assert f"retry in {wait} via the {key} queue" in caplog.text


def test_check_run_cancellation_logs_run_and_reason_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RetiredStore(FakeStore):
        async def provider_origin_active(self, run_id: UUID) -> bool:
            return False

    store = RetiredStore(FakeRun(state="running", attempt=1))
    with caplog.at_level("INFO"):
        asyncio.run(update_check_run(lambda: FakeUow(store), CheckRuns(), RUN, lambda _: None))
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        (
            "INFO",
            "Check-run update cancelled for run "
            "11111111-1111-1111-1111-111111111111: installation_unavailable",
        )
    ]


def test_check_run_update_passes_the_run_url_to_the_view_as_details_url() -> None:
    store = FakeStore(FakeRun())
    check_runs = CheckRuns()

    asyncio.run(
        update_check_run(
            lambda: FakeUow(store),
            check_runs,
            RUN,
            lambda run_id: f"https://review.example/runs/{run_id}",
        )
    )

    assert [view.details_url for view in check_runs.views] == [
        "https://review.example/runs/11111111-1111-1111-1111-111111111111"
    ]


def test_failed_commit_fetch_check_run_names_commit_messages() -> None:
    view = check_run_view(
        CheckRunReport(
            CheckRunTarget(17, "octo/repo", HEAD, RUN),
            "failed",
            3,
            "commit_messages_fetch_failed",
        )
    )
    assert (view.status, view.conclusion, view.title) == (
        "completed",
        "neutral",
        "AI-ревью не выполнено",
    )
    assert view.summary == (
        "`commit_messages_fetch_failed`: Не удалось получить сообщения коммитов PR из GitHub."
    )


@pytest.mark.parametrize(
    ("retry_after", "queues", "waits"),
    [
        (None, ("30s", "2m"), (30, 120)),
        (timedelta(seconds=120), ("2m", "2m"), (120, 120)),
        (timedelta(seconds=300), ("10m", "10m"), (600, 600)),
    ],
)
def test_commit_fetch_failures_retry_then_finish_with_commit_error(
    retry_after: timedelta | None, queues: tuple[str, str], waits: tuple[int, int]
) -> None:
    run = FakeRun()
    retry = RetryQueue()
    check_runs = CheckRuns()
    clock = Clock()
    outcomes: list[DeliveryOutcome] = []
    due_dates: list[datetime] = []

    def failing(claimed: ClaimedAttempt) -> bool:
        raise RunFailure(
            "commit_messages_fetch_failed", "GitHub answered 502", retry_after=retry_after
        )

    for _ in range(3):
        clock.value = run.available_at
        outcomes.append(
            asyncio.run(
                handler(run, failing, retry=retry, check_runs=check_runs, clock=clock).execute(RUN)
            )
        )
        due_dates.append(run.available_at)

    assert outcomes == [DeliveryOutcome.ACK, DeliveryOutcome.ACK, DeliveryOutcome.DEAD_LETTER]
    assert retry.published == [(queues[0], 2), (queues[1], 3)]
    assert due_dates[:2] == [
        NOW + timedelta(seconds=waits[0]),
        NOW + timedelta(seconds=waits[0] + waits[1]),
    ]
    assert (run.state, run.error_code, run.error_message, run.attempt) == (
        "failed",
        "commit_messages_fetch_failed",
        "GitHub answered 502",
        3,
    )
    assert check_runs.views[-1].summary == (
        "`commit_messages_fetch_failed`: Не удалось получить сообщения коммитов PR из GitHub."
    )


def test_commit_fetch_gateway_code_is_recognized_by_failure_classifier() -> None:
    from app.modules.reviews.application.run_failures import classify_failure

    class CommitGatewayFailure(Exception):
        error_code = "commit_messages_fetch_failed"

    failure = classify_failure(CommitGatewayFailure("GitHub answered 502"))
    assert failure.error_code == "commit_messages_fetch_failed"
    assert failure.message == "GitHub answered 502"


def test_verification_error_codes_classification_and_check_runs() -> None:
    from app.modules.reviews.application.check_runs import check_run_view
    from app.modules.reviews.application.reflection_pass import (
        VerificationInvalidOutputError,
        VerificationUnavailableError,
    )
    from app.modules.reviews.application.run_failures import classify_failure

    # Class error_code attributes
    assert VerificationInvalidOutputError.error_code == "verification_invalid_output"
    assert VerificationUnavailableError.error_code == "verification_unavailable"

    # classify_failure through KNOWN_ERROR_CODES catalog using real exception instances
    class CustomVerificationError(Exception):
        def __init__(self, error_code: str, message: str) -> None:
            super().__init__(message)
            self.error_code = error_code

    # Real canonical exception instances
    inv_out_fail = classify_failure(VerificationInvalidOutputError("Invalid verifier JSON"))
    assert inv_out_fail.error_code == "verification_invalid_output"
    assert inv_out_fail.error_code not in RETRYABLE_ERROR_CODES

    unavail_real = classify_failure(VerificationUnavailableError("Timeout"))
    assert unavail_real.error_code == "verification_unavailable"
    assert unavail_real.error_code in RETRYABLE_ERROR_CODES

    terminal_codes = [
        "verification_invalid_output",
        "verification_context_overflow",
        "verification_budget_exceeded",
        "verification_deadline_exceeded",
    ]
    for code in terminal_codes:
        fail = classify_failure(CustomVerificationError(code, "Details"))
        assert fail.error_code == code
        assert fail.error_code not in RETRYABLE_ERROR_CODES

    unavail_fail = classify_failure(CustomVerificationError("verification_unavailable", "Timeout"))
    assert unavail_fail.error_code == "verification_unavailable"
    assert unavail_fail.error_code in RETRYABLE_ERROR_CODES

    # Check run summary translations
    target = CheckRunTarget(1, "octo/repo", HEAD, RUN)
    expected_texts = {
        "verification_context_overflow": "Обязательные данные проверки не помещаются в контекст.",
        "verification_invalid_output": "Проверяющий вернул ответ не по контракту.",
        "verification_unavailable": "Проверяющий временно недоступен.",
        "verification_budget_exceeded": "Бюджета недостаточно для обязательной проверки.",
        "verification_deadline_exceeded": "Недостаточно времени для обязательной проверки.",
    }
    for code, expected_summary in expected_texts.items():
        report = CheckRunReport(target, "failed", 1, code)
        view = check_run_view(report)
        assert expected_summary in view.summary
        assert f"`{code}`" in view.summary


def _provider_retry_after_failure(
    retry_after: str,
    *,
    status: int = 403,
    commits: bool = False,
    remaining: str | None = None,
    date: str | None = None,
) -> RunFailure:
    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "fake-installation-token"

    async def read() -> None:
        headers = [(b"Retry-After", retry_after.encode("utf-8"))]
        if remaining is not None:
            headers.append((b"X-RateLimit-Remaining", remaining.encode("ascii")))
        if date is not None:
            headers.append((b"Date", date.encode("ascii")))
        async with httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(status, headers=headers)),
        ) as client:
            provider = ClassifiedVcsProvider(
                HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            )

            async def checkpoint() -> None:
                return None

            locator = PullRequestLocator(17, "octo/repo", 7)
            if commits:
                await provider.get_commit_messages(locator, checkpoint=checkpoint)
            else:
                await provider.get_pull_request(locator, checkpoint=checkpoint)

    with pytest.raises(RunFailure) as caught:
        asyncio.run(read())
    return caught.value


@pytest.mark.parametrize("commits", [False, True])
@pytest.mark.parametrize(
    "header",
    [
        # Representable, yet beyond a day: the shared parser caps it like the policy read.
        pytest.param("86399999999999", id="largest-representable-seconds"),
        pytest.param("86400000000000", id="timedelta-overflow"),
        pytest.param("9" * 5000, id="integer-digit-limit"),
    ],
)
def test_oversized_numeric_retry_after_keeps_rate_limit_retry_in_the_long_bucket(
    header: str, commits: bool
) -> None:
    failure = _provider_retry_after_failure(header, commits=commits)

    assert failure.error_code == (
        "commit_messages_fetch_failed" if commits else "diff_fetch_failed"
    )
    # A huge wait is never shorter than a merely large one: it takes the one-day ceiling.
    assert failure.retry_after == timedelta(days=1)
    assert RetryDelays().for_failure(1, failure) == ("10m", timedelta(days=1))


@pytest.mark.parametrize("commits", [False, True])
@pytest.mark.parametrize(
    "header",
    [
        pytest.param("", id="empty"),
        pytest.param("later", id="alphabetic"),
        pytest.param("-1", id="negative"),
        pytest.param("+120", id="positive-sign"),
        pytest.param("120.0", id="fractional"),
        pytest.param(" 120", id="whitespace"),
        pytest.param("²", id="superscript-digit"),
        pytest.param("١٢٠", id="unicode-decimal"),
    ],
)
def test_malformed_retry_after_preserves_forbidden_and_retryable_classification(
    header: str, commits: bool
) -> None:
    forbidden = _provider_retry_after_failure(header, status=403, commits=commits)
    retryable = _provider_retry_after_failure(header, status=429, commits=commits)

    assert (forbidden.error_code, forbidden.retry_after) == ("github_forbidden", None)
    assert retryable.error_code == (
        "commit_messages_fetch_failed" if commits else "diff_fetch_failed"
    )
    assert retryable.retry_after is None


@pytest.mark.parametrize("commits", [False, True])
@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize(
    ("header", "delay", "queue"),
    [
        pytest.param(
            "Wed, 21 Oct 2026 07:28:00 GMT", timedelta(seconds=120), "2m", id="two-minutes"
        ),
        pytest.param(
            "Wed, 21 Oct 2026 07:31:00 GMT", timedelta(seconds=300), "10m", id="five-minutes"
        ),
        # Already past at the response Date: no wait, and still a limit, not a lost access.
        pytest.param("Wed, 21 Oct 2026 07:25:00 GMT", timedelta(0), "2m", id="past"),
    ],
)
def test_http_date_retry_after_is_a_retryable_limit_with_its_delay(
    header: str, delay: timedelta, queue: str, status: int, commits: bool
) -> None:
    failure = _provider_retry_after_failure(
        header, status=status, commits=commits, date="Wed, 21 Oct 2026 07:26:00 GMT"
    )

    assert failure.error_code == (
        "commit_messages_fetch_failed" if commits else "diff_fetch_failed"
    )
    assert failure.retry_after == delay
    assert RetryDelays().for_failure(1, failure)[0] == queue


@pytest.mark.parametrize("commits", [False, True])
@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize(
    ("header", "delay", "queue"),
    [
        pytest.param("0", timedelta(0), "2m", id="zero"),
        pytest.param("120", timedelta(seconds=120), "2m", id="two-minutes"),
        pytest.param("300", timedelta(seconds=300), "10m", id="five-minutes"),
        pytest.param("000120", timedelta(seconds=120), "2m", id="leading-zeros"),
        pytest.param("0" * 5000 + "300", timedelta(seconds=300), "10m", id="many-leading-zeros"),
        pytest.param("0" * 5000, timedelta(0), "2m", id="many-zero-digits"),
    ],
)
def test_representable_numeric_retry_after_preserves_delay_and_existing_queue(
    header: str, delay: timedelta, queue: str, status: int, commits: bool
) -> None:
    failure = _provider_retry_after_failure(header, status=status, commits=commits)

    assert failure.error_code == (
        "commit_messages_fetch_failed" if commits else "diff_fetch_failed"
    )
    assert failure.retry_after == delay
    assert RetryDelays().for_failure(1, failure)[0] == queue


@pytest.mark.parametrize("commits", [False, True])
@pytest.mark.parametrize(
    "header",
    [
        pytest.param("later", id="malformed"),
        pytest.param("120", id="ordinary"),
        pytest.param("9" * 5000, id="oversized"),
    ],
)
def test_not_found_remains_forbidden_regardless_of_retry_after(header: str, commits: bool) -> None:
    failure = _provider_retry_after_failure(header, status=404, commits=commits)

    assert (failure.error_code, failure.retry_after) == ("github_forbidden", None)


@pytest.mark.parametrize("commits", [False, True])
def test_exhausted_github_rate_limit_remains_retryable_with_malformed_retry_after(
    commits: bool,
) -> None:
    failure = _provider_retry_after_failure("later", remaining="0", commits=commits)

    assert failure.error_code == (
        "commit_messages_fetch_failed" if commits else "diff_fetch_failed"
    )
    assert failure.retry_after is None


# The message GitHub sends with a secondary rate limit; only its "secondary rate limit" counts.
_SECONDARY_LIMIT = {
    "message": "You have exceeded a secondary rate limit. Please wait a few minutes before you "
    "try again.",
    "documentation_url": "https://docs.github.com/rest/using-the-rest-api/rate-limits-for-the-rest-api",
}
_PULL = {
    "id": 901,
    "number": 7,
    "title": "Fix parser",
    "user": {"login": "octo"},
    "head": {"ref": "feature", "sha": HEAD, "repo": {"full_name": "octo/repo"}},
    "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "octo/repo"}},
    "draft": False,
    "changed_files": 1,
    "additions": 1,
    "deletions": 0,
}


def _github_read_failure(read: str, status: int, body: object) -> RunFailure:
    """The failure of one run read whose GitHub answer has ``body`` and no limit headers."""

    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "fake-installation-token"

    def respond(request: httpx.Request) -> httpx.Response:
        if read == "diff" and request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_PULL)
        if isinstance(body, httpx.AsyncByteStream):
            return httpx.Response(status, stream=body)
        return httpx.Response(status, json=body)

    async def checkpoint() -> None:
        return None

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ) as client:
            provider = ClassifiedVcsProvider(
                HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            )
            locator = PullRequestLocator(17, "octo/repo", 7)
            if read == "commits":
                await provider.get_commit_messages(locator, checkpoint=checkpoint)
                return
            pull_request = await provider.get_pull_request(locator, checkpoint=checkpoint)
            await provider.get_diff(pull_request, checkpoint=checkpoint)

    with pytest.raises(RunFailure) as caught:
        asyncio.run(exercise())
    return caught.value


_FETCH_FAILED = {
    "pull": "diff_fetch_failed",
    "diff": "diff_fetch_failed",
    "commits": "commit_messages_fetch_failed",
}


@pytest.mark.parametrize("read", ["pull", "diff", "commits"])
@pytest.mark.parametrize("status", [403, 429])
def test_secondary_rate_limit_without_headers_is_a_retryable_limit_of_a_minute(
    read: str, status: int
) -> None:
    failure = _github_read_failure(read, status, _SECONDARY_LIMIT)

    assert (failure.error_code, failure.retry_after) == (
        _FETCH_FAILED[read],
        timedelta(seconds=60),
    )
    assert RetryDelays().for_failure(1, failure) == ("2m", timedelta(minutes=2))


@pytest.mark.parametrize("read", ["pull", "diff", "commits"])
def test_forbidden_without_the_secondary_limit_message_stays_forbidden(read: str) -> None:
    failure = _github_read_failure(read, 403, {"message": "Resource not accessible by integration"})

    assert (failure.error_code, failure.retry_after) == ("github_forbidden", None)


@pytest.mark.parametrize("read", ["pull", "diff", "commits"])
def test_too_many_requests_without_headers_or_message_keeps_no_delay(read: str) -> None:
    # As before #168 part 6: retryable, yet with no wait of its own.
    failure = _github_read_failure(read, 429, {"message": "Too many requests"})

    assert (failure.error_code, failure.retry_after) == (_FETCH_FAILED[read], None)
    assert RetryDelays().for_failure(1, failure) == ("30s", timedelta(seconds=30))


class _CountedBody(httpx.AsyncByteStream):
    """A body sent in chunks of 1000 bytes; counts the bytes handed out."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), 1000):
            piece = self._body[start : start + 1000]
            self.sent += len(piece)
            yield piece


def test_commit_message_stream_reads_at_most_4_kib_of_an_error_body() -> None:
    body = _CountedBody(b" " * 1_048_576)

    failure = _github_read_failure("commits", 403, body)

    assert (failure.error_code, failure.retry_after) == ("github_forbidden", None)
    # The read stops with the first chunk that reaches 4 KiB: five chunks of 1000 bytes.
    assert body.sent == 5000


@pytest.mark.parametrize("provider", ["gitlab", "unknown"])
@pytest.mark.parametrize(
    "state", ["queued", "running", "publishing", "succeeded", "failed", "cancelled", "skipped"]
)
def test_public_check_updater_suppresses_foreign_provider_before_any_github_work(
    provider: str, state: str
) -> None:
    urls: list[UUID] = []
    checkpoints: list[UUID] = []

    class ProviderStore(FakeStore):
        async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
            return CheckRunReport(
                CheckRunTarget(17, "octo/repo", HEAD, run_id), state, 1, provider=provider
            )

        async def provider_origin_active(self, run_id: UUID) -> bool:
            checkpoints.append(run_id)
            return True

    def run_url(run_id: UUID) -> str:
        urls.append(run_id)
        return "https://review.example/runs/foreign"

    checks = CheckRuns()
    asyncio.run(update_check_run(lambda: FakeUow(ProviderStore(FakeRun())), checks, RUN, run_url))

    assert checks.views == []
    assert urls == []
    assert checkpoints == []


def test_actual_checkpointed_provider_keeps_required_source_control_identity() -> None:
    from app.modules.reviews.application.conventions import RepositoryTreeResult
    from app.modules.reviews.application.prompt_builder import PullRequestMeta
    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable
    from app.worker import AttemptReviewProvider, UnconfiguredModel
    from tests.test_repo_conventions import REPOSITORY_ID, FakeSource

    failure = RequiredSourceUnavailable("diff_fetch_failed")

    class Source(FakeSource):
        async def fetch_tree(self, repository_id: UUID) -> RepositoryTreeResult:
            raise failure

        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
            return None

    async def checkpoint() -> None:
        raise AssertionError("only model calls add the model checkpoint")

    wrapped = CheckpointedProvider(
        AttemptReviewProvider(Source(), UnconfiguredModel(), UnconfiguredModel()), checkpoint
    )
    with pytest.raises(RequiredSourceUnavailable) as caught:
        asyncio.run(wrapped.fetch_tree(REPOSITORY_ID))
    assert caught.value is failure
