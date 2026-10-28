"""The real worker composition supplies links to both check-run publication paths."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker
from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.determine_ci_eligibility import CiEligibility
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    DeliveryOutcome,
    RunGuardSnapshot,
)
from app.modules.reviews.application.publish_run_review import (
    PublishContext,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource

RUN = UUID("11111111-2222-3333-4444-555555555555")
NOW = datetime(2026, 10, 25, tzinfo=UTC)
HEAD = "a" * 40
HASH = "f" * 64


@dataclass
class Store:
    state: str
    open_units: int = 0
    provider_observations: list[tuple[str, int]] = field(default_factory=list)
    submissions: list[ReviewSubmission] = field(default_factory=list)
    views: list[tuple[CheckRunTarget, CheckRunView]] = field(default_factory=list)

    def assert_transaction(self, run_id: UUID) -> None:
        assert run_id == RUN
        assert self.open_units == 1

    async def lock_for_claim(self, run_id: UUID) -> RunGuardSnapshot:
        self.assert_transaction(run_id)
        assert self.state == "failed"
        return RunGuardSnapshot(
            run_id=RUN,
            workspace_id=UUID(int=2),
            state="failed",
            attempt=1,
            available_at=NOW,
            cancel_requested=False,
            head_sha=HEAD,
            pr_head_sha=HEAD,
            pr_open=True,
            repository_enabled=True,
            daily_budget_usd=Decimal("0"),
            spent_today_usd=Decimal("0"),
            engine="fast",
            prompt_version_id=UUID(int=3),
            rule_version_id=UUID(int=4),
        )

    async def publish_context(self, run_id: UUID) -> PublishContext:
        self.assert_transaction(run_id)
        assert self.state == "publishing"
        return PublishContext(
            run_id=RUN,
            state="publishing",
            cancel_requested=False,
            head_sha=HEAD,
            pr_head_sha=HEAD,
            pr_open=True,
            origin_active=True,
            installation_id=17,
            repository_full_name="octo/repo",
            pr_number=7,
            review_body="## Review summary",
            findings=(),
            findings_hash=HASH,
            provider="github",
            project_id=101,
        )

    async def complete_publication(
        self,
        run_id: UUID,
        *,
        review_id: int,
        comment_ids: tuple[int, ...],
        findings_hash: str,
        moved_to_body_finding_ids: tuple[UUID, ...] = (),
        now: datetime,
        expected_publication: PublishContext | None = None,
    ) -> str:
        assert expected_publication is None
        self.assert_transaction(run_id)
        assert (review_id, comment_ids, findings_hash, moved_to_body_finding_ids, now) == (
            55,
            (),
            HASH,
            (),
            NOW,
        )
        self.state = "succeeded"
        return "succeeded"

    async def provider_origin_active(self, run_id: UUID) -> bool:
        self.assert_transaction(run_id)
        return True

    async def check_run_report(self, run_id: UUID) -> CheckRunReport:
        self.assert_transaction(run_id)
        return CheckRunReport(
            target=CheckRunTarget(17, "octo/repo", HEAD, RUN),
            state=self.state,
            attempt=1,
            error_code="internal_error" if self.state == "failed" else None,
            verdict="clean" if self.state == "succeeded" else None,
        )

    async def claim(
        self, run_id: UUID, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> int:
        raise AssertionError("terminal recovery must not claim a review attempt")

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
        expected_publication: PublishContext | None = None,
    ) -> bool:
        raise AssertionError("neither selected success path finishes a failed publication")

    async def extend_lease(self, run_id: UUID, worker_id: str, lease_until: datetime) -> bool:
        raise AssertionError("terminal recovery must not extend a lease")

    async def requeue(self, run_id: UUID, *, worker_id: str | None, available_at: datetime) -> bool:
        raise AssertionError("terminal recovery must not requeue")

    async def cancellation_reason(self, run_id: UUID) -> str | None:
        raise AssertionError("terminal recovery must not start attempt checkpoints")

    async def run_message(self, run_id: UUID) -> PendingRunMessage | None:
        raise AssertionError("terminal recovery must not publish another run")

    async def add_action(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        self.assert_transaction(run_id)
        assert tool == "github.publish_review"

    async def save_context_payload(
        self, run_id: UUID, summary: dict[str, Any], *, mark_summary_only: bool = False
    ) -> None:
        raise AssertionError("these paths must not build review context")


class Uow:
    def __init__(self, store: Store) -> None:
        self.runs = store
        self.trace = store

    async def __aenter__(self) -> Self:
        assert self.runs.open_units == 0
        self.runs.open_units += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.runs.open_units -= 1

    async def commit(self) -> None:
        assert self.runs.open_units == 1

    async def rollback(self) -> None:
        raise AssertionError("these success paths do not roll back")


@dataclass
class CheckRuns:
    store: Store

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        self.store.provider_observations.append(("check-run:before", self.store.open_units))
        await checkpoint()
        self.store.provider_observations.append(("check-run:after", self.store.open_units))
        self.store.views.append((target, view))


@dataclass
class Reviews:
    store: Store

    async def submit_review(
        self, submission: ReviewSubmission, *, checkpoint: RunProviderCheckpoint
    ) -> SubmittedReview:
        self.store.provider_observations.append(("review:before", self.store.open_units))
        await checkpoint()
        self.store.provider_observations.append(("review:after", self.store.open_units))
        self.store.submissions.append(submission)
        return SubmittedReview(55, ())


class UnusedAdapters:
    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        raise AssertionError("these paths must not read a pull request")

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        raise AssertionError("these paths must not read a diff")

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        raise AssertionError("these paths must not read a blob")

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        raise AssertionError("these paths must not check CI eligibility")

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        raise AssertionError("these paths must not enqueue a run")

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        raise AssertionError("these paths must not retry a run")

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        raise AssertionError("these paths must not enqueue a publication")

    def run_source(self, run_id: UUID) -> GitHubRunSource:
        raise AssertionError("these paths must not construct a review provider")

    def provider(self, claimed: ClaimedAttempt) -> worker.ReviewWorkerProvider:
        raise AssertionError("terminal recovery must not execute the review pipeline")


class Selection:
    async def matches(self, run_id: UUID) -> bool:
        assert run_id == RUN
        return True


def compose(
    monkeypatch: pytest.MonkeyPatch, store: Store, portal_url: str | None
) -> worker.WorkerProcess:
    session_factory = async_sessionmaker[AsyncSession]()

    def uow_factory(received: async_sessionmaker[AsyncSession]) -> Uow:
        assert received is session_factory
        return Uow(store)

    def selection_factory(received: async_sessionmaker[AsyncSession]) -> Selection:
        assert received is session_factory
        return Selection()

    monkeypatch.setattr(worker, "SqlAlchemyRunLifecycleUnitOfWork", uow_factory)
    monkeypatch.setattr(worker, "SqlAlchemyRunTraceUnitOfWork", uow_factory)
    monkeypatch.setattr(worker, "SqlAlchemyBranchSelectionRule", selection_factory)
    unused = UnusedAdapters()
    return worker.compose_worker_process(
        settings=worker.WorkerSettings.from_environment(
            {
                "DATABASE_URL": "postgresql+psycopg://unused/unused",
                "RABBITMQ_URL": "amqp://unused/",
                **({"PORTAL_URL": portal_url} if portal_url is not None else {}),
            }
        ),
        session_factory=session_factory,
        queue=unused,
        github=worker.GitHubAdapters(
            vcs=unused,
            check_runs=CheckRuns(store),
            reviews=Reviews(store),
            eligibility=unused,
            run_source=unused.run_source,
        ),
        provider_factory=unused.provider,
        now=lambda: NOW,
    )


_PORTAL_CASES = [
    pytest.param(
        "https://portal.test///",
        "https://portal.test/runs/11111111-2222-3333-4444-555555555555",
        "\n\nhttps://portal.test/runs/11111111-2222-3333-4444-555555555555",
        id="linked",
    ),
    pytest.param(None, None, None, id="unset"),
    pytest.param("", None, None, id="empty"),
]


@pytest.mark.parametrize(("portal_url", "expected_url", "expected_summary_suffix"), _PORTAL_CASES)
def test_composed_handle_run_recovery_publishes_portal_link(
    monkeypatch: pytest.MonkeyPatch,
    portal_url: str | None,
    expected_url: str | None,
    expected_summary_suffix: str | None,
) -> None:
    store = Store("failed")
    process = compose(monkeypatch, store, portal_url)

    assert asyncio.run(process.handle_run.execute(RUN)) == DeliveryOutcome.ACK

    assert store.provider_observations == [("check-run:before", 0), ("check-run:after", 0)]
    assert len(store.views) == 1
    target, view = store.views[0]
    assert target.run_id == UUID("11111111-2222-3333-4444-555555555555")
    assert view.status == "completed"
    assert view.details_url == expected_url
    if expected_summary_suffix is None:
        assert "/runs/" not in view.summary
    else:
        assert view.summary.endswith(expected_summary_suffix)
    assert store.open_units == 0
    assert store.submissions == []


@pytest.mark.parametrize(("portal_url", "expected_url", "expected_summary_suffix"), _PORTAL_CASES)
def test_composed_publish_review_success_publishes_portal_link(
    monkeypatch: pytest.MonkeyPatch,
    portal_url: str | None,
    expected_url: str | None,
    expected_summary_suffix: str | None,
) -> None:
    store = Store("publishing")
    process = compose(monkeypatch, store, portal_url)

    asyncio.run(process.publish_review.execute(ReviewPublishPointer(RUN, HEAD, HASH, "COMMENT")))

    assert store.provider_observations == [
        ("review:before", 0),
        ("review:after", 0),
        ("check-run:before", 0),
        ("check-run:after", 0),
    ]
    assert store.state == "succeeded"
    assert len(store.submissions) == 1
    assert store.submissions[0].commit_sha == HEAD
    assert len(store.views) == 1
    target, view = store.views[0]
    assert target.run_id == UUID("11111111-2222-3333-4444-555555555555")
    assert view.status == "completed"
    assert view.details_url == expected_url
    if expected_summary_suffix is None:
        assert "/runs/" not in view.summary
    else:
        assert view.summary.endswith(expected_summary_suffix)
    assert store.open_units == 0
