"""Verified GitHub webhook receipts are durable before projection."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import (
    _has_valid_github_signature,
    app,
    get_github_webhook_secret,
)
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    GitHubInstallationResolver,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    InstallationOnboardingHandler,
    PullRequestLabelIntentHandler,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    WebhookReceipt,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestProjectionStatus,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent

_SECRET = "receipt-test-secret"


@dataclass
class FakeDispatcher:
    deliveries: list[WebhookReceipt] = field(default_factory=list)

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        self.deliveries.append(delivery)
        return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)


@dataclass
class FakeClaimedReceipt:
    delivery: WebhookReceipt
    projected: bool = False
    projection_attempt_count: int = 0
    projection_failed_at: datetime | None = None
    projection_deferred_at: datetime | None = None
    claim_token: object | None = None
    lease_until: datetime | None = None
    retry_after: datetime | None = None


@dataclass
class FakeReceiptUnitOfWork:
    rows: dict[str, FakeClaimedReceipt] = field(default_factory=dict)
    commits: int = 0

    @property
    def receipts(self) -> FakeReceiptUnitOfWork:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def save(self, delivery: WebhookReceipt) -> bool:
        if delivery.delivery_id in self.rows:
            return False
        self.rows[delivery.delivery_id] = FakeClaimedReceipt(delivery)
        return True

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> WebhookReceipt | None:
        row = self.rows[delivery_id]
        if (
            row.projected
            or row.projection_failed_at is not None
            or row.projection_deferred_at is not None
            or (row.lease_until is not None and row.lease_until > now)
        ):
            return None
        if row.retry_after is not None and row.retry_after > now:
            return None
        row.claim_token = token
        row.lease_until = until
        return row.delivery

    async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.projected = True
        row.claim_token = None
        row.lease_until = None

    async def release(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        deferred_at: datetime,
        max_attempts: int,
    ) -> bool:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.projection_attempt_count += 1
        row.claim_token = None
        row.lease_until = None
        if row.projection_attempt_count >= max_attempts:
            row.projection_deferred_at = deferred_at
            row.retry_after = None
            return True
        row.retry_after = retry_after
        return False

    async def purge_finished(self, before: datetime) -> int:
        return 0

    async def release_after_dispatch_failure(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        failed_at: datetime,
        max_attempts: int,
    ) -> None:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.projection_attempt_count += 1
        row.claim_token = None
        row.lease_until = None
        if row.projection_attempt_count == max_attempts:
            row.projection_failed_at = failed_at
            row.retry_after = None
        else:
            row.retry_after = retry_after

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        return tuple(
            delivery_id
            for delivery_id, row in self.rows.items()
            if not row.projected
            and row.projection_failed_at is None
            and row.projection_deferred_at is None
            and (row.lease_until is None or row.lease_until <= now)
            and (row.retry_after is None or row.retry_after <= now)
        )[:limit]


def test_failed_dispatch_is_replayed_once_after_retry_delay() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    delivery = VerifiedGitHubDelivery("retry-1", "installation", {"action": "created"})
    assert delivery.to_receipt() == WebhookReceipt(
        "retry-1", "installation", '{"action": "created"}'
    )

    @dataclass
    class FailOnceDispatcher:
        calls: int = 0

        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("GitHub unavailable")
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    dispatcher = FailOnceDispatcher()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=dispatcher, now=lambda: now
    )

    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )
    assert dispatcher.calls == 0
    assert asyncio.run(receiver.replay_pending()) == 0
    assert dispatcher.calls == 1
    assert uow.rows["retry-1"].projected is False
    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.DUPLICATE
    )
    assert dispatcher.calls == 1

    now += timedelta(seconds=31)
    assert asyncio.run(receiver.replay_pending()) == 1
    assert uow.rows["retry-1"].projected is True
    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.DUPLICATE
    )
    assert dispatcher.calls == 2


def test_failed_dispatch_stops_after_three_total_attempts() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    delivery = VerifiedGitHubDelivery("retry-limit-1", "installation", {"action": "created"})
    uow = FakeReceiptUnitOfWork()

    @dataclass
    class AlwaysFailDispatcher:
        calls: int = 0

        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            self.calls += 1
            raise RuntimeError("GitHub unavailable")

    dispatcher = AlwaysFailDispatcher()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=dispatcher, now=lambda: now
    )
    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )

    for attempt in range(1, 4):
        assert asyncio.run(receiver.replay_pending()) == 0
        row = uow.rows["retry-limit-1"]
        assert row.projection_attempt_count == attempt
        if attempt < 3:
            assert row.projection_failed_at is None
            assert row.retry_after == now + timedelta(seconds=30)
            now += timedelta(seconds=31)
        else:
            assert row.projection_failed_at == now
            assert row.retry_after is None

    now += timedelta(days=1)
    assert asyncio.run(receiver.replay_pending()) == 0
    assert dispatcher.calls == 3


def test_replay_claims_receipt_left_by_crash_after_commit() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    delivery = VerifiedGitHubDelivery("crash-1", "ping", {"zen": "hello"})
    uow = FakeReceiptUnitOfWork(rows={"crash-1": FakeClaimedReceipt(delivery.to_receipt())})
    dispatcher = FakeDispatcher()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=dispatcher, now=lambda: now
    )

    assert asyncio.run(receiver.replay_pending()) == 1
    assert asyncio.run(receiver.replay_pending()) == 0
    assert dispatcher.deliveries == [delivery.to_receipt()]


def test_malformed_supported_receipt_keeps_raw_json_and_is_acknowledged_on_replay() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    raw = {"action": "opened", "installation": {"id": 17}, "extra": {"kept": True}}
    delivery = VerifiedGitHubDelivery("malformed-pr", "pull_request", raw)
    uow = FakeReceiptUnitOfWork()
    dispatcher = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
        )
    )
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=dispatcher, now=lambda: now
    )

    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )
    assert json.loads(uow.rows["malformed-pr"].delivery.payload_json) == raw
    assert uow.rows["malformed-pr"].projected is False
    assert asyncio.run(receiver.replay_pending()) == 1
    assert uow.rows["malformed-pr"].projected is True
    assert asyncio.run(receiver.replay_pending()) == 0


def test_exact_label_receipt_stays_raw_and_retries_until_projector_and_trigger_exist() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    raw: dict[str, object] = {
        "action": "labeled",
        "label": {"name": "ai-review"},
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Review parser",
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
            "state": "open",
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }
    delivery = VerifiedGitHubDelivery("label-retry", "pull_request", raw)
    uow = FakeReceiptUnitOfWork()

    class Trigger:
        async def on_label(self, event: PullRequestLabelEvent) -> None:
            assert event.label_name == "ai-review"

        async def on_pr(self, event: PullRequestEvent) -> None:
            raise AssertionError("label should not use PR trigger")

        async def on_ci(self, event: CiTriggerEvent) -> None:
            raise AssertionError("label should not use CI trigger")

    def receiver(intent: object | None = None) -> ReceiveGitHubDelivery:
        return ReceiveGitHubDelivery(
            uow_factory=lambda: uow,
            dispatcher=GitHubWebhookDispatchAdapter(
                GitHubInstallationDeliveryDispatcher(
                    resolver=cast(GitHubInstallationResolver, None),
                    onboarding=cast(InstallationOnboardingHandler, None),
                    label_intent_projector=cast(PullRequestLabelIntentHandler | None, intent),
                    run_trigger=Trigger() if intent is not None else None,
                )
            ),
            now=lambda: now,
        )

    first = receiver()
    assert (
        asyncio.run(first.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )
    assert json.loads(uow.rows["label-retry"].delivery.payload_json) == raw
    assert asyncio.run(first.replay_pending()) == 1
    assert uow.rows["label-retry"].projected is False

    @dataclass
    class Intent:
        events: list[PullRequestLabelEvent] = field(default_factory=list)

        async def execute(self, event: PullRequestLabelEvent) -> PullRequestProjectionStatus:
            self.events.append(event)
            return PullRequestProjectionStatus.PROJECTED

    intent = Intent()
    now += timedelta(minutes=5, seconds=1)
    assert asyncio.run(receiver(intent).replay_pending()) == 1
    assert uow.rows["label-retry"].projected is True
    assert json.loads(uow.rows["label-retry"].delivery.payload_json) == raw
    assert len(intent.events) == 1
    assert intent.events[0].label_name == "ai-review"


@pytest.mark.parametrize(
    ("projection", "dispatch_status", "acknowledged"),
    [
        (
            PullRequestProjectionStatus.UNKNOWN_REPOSITORY,
            InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY,
            False,
        ),
        (
            PullRequestProjectionStatus.IGNORED_STALE,
            InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
            False,
        ),
        (
            PullRequestProjectionStatus.IGNORED_UNRELATED,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            True,
        ),
    ],
)
def test_label_projection_status_controls_receipt_ack_or_retry(
    projection: PullRequestProjectionStatus,
    dispatch_status: InstallationDeliveryDispatchStatus,
    acknowledged: bool,
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    delivery = VerifiedGitHubDelivery(
        "label-result",
        "pull_request",
        {
            "action": "labeled",
            "label": {"name": "ai-review"},
            "installation": {"id": 17},
            "repository": {"id": 101, "full_name": "octo/repo"},
            "pull_request": {
                "id": 901,
                "number": 7,
                "title": "Review parser",
                "html_url": "https://github.com/octo/repo/pull/7",
                "user": {"login": "alice"},
                "head": {"ref": "feature", "sha": "a" * 40},
                "base": {"ref": "main", "sha": "b" * 40},
                "state": "open",
                "updated_at": "2026-09-28T11:59:00Z",
            },
        },
    )

    @dataclass
    class Intent:
        calls: int = 0

        async def execute(self, event: PullRequestLabelEvent) -> PullRequestProjectionStatus:
            assert event.label_name == "ai-review"
            self.calls += 1
            return projection

    intent = Intent()
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
            label_intent_projector=intent,
        )
    )
    assert asyncio.run(adapter.execute(delivery.to_receipt())).status is dispatch_status
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(uow_factory=lambda: uow, dispatcher=adapter, now=lambda: now)
    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )
    assert asyncio.run(receiver.replay_pending()) == 1
    row = uow.rows["label-result"]
    assert row.projected is acknowledged
    assert row.retry_after == (None if acknowledged else now + timedelta(minutes=5))
    assert asyncio.run(receiver.replay_pending()) == 0
    if not acknowledged:
        now += timedelta(minutes=5, seconds=1)
        assert asyncio.run(receiver.replay_pending()) == 1
        assert intent.calls == 3


def test_expired_claim_is_replayed_but_live_claim_is_not() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    delivery = VerifiedGitHubDelivery("leased-1", "ping", {"zen": "hello"})
    row = FakeClaimedReceipt(
        delivery.to_receipt(),
        claim_token=uuid4(),
        lease_until=now + timedelta(minutes=5),
    )
    uow = FakeReceiptUnitOfWork(rows={"leased-1": row})
    dispatcher = FakeDispatcher()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=dispatcher, now=lambda: now
    )

    assert asyncio.run(receiver.replay_pending()) == 0
    now += timedelta(minutes=6)
    assert asyncio.run(receiver.replay_pending()) == 1
    assert row.projected is True
    assert dispatcher.deliveries == [delivery.to_receipt()]


def test_dispatch_timeout_releases_claim_before_lease_expiry() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    delivery = VerifiedGitHubDelivery("timeout-1", "ping", {"zen": "hello"})
    uow = FakeReceiptUnitOfWork(rows={"timeout-1": FakeClaimedReceipt(delivery.to_receipt())})

    class SlowDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            await asyncio.sleep(0.05)
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=SlowDispatcher(),
        now=lambda: now,
        dispatch_timeout_seconds=0.001,
    )

    assert asyncio.run(receiver.replay_pending()) == 0
    row = uow.rows["timeout-1"]
    assert row.projected is False
    assert row.claim_token is None
    assert row.lease_until is None
    assert row.retry_after == now + timedelta(seconds=30)


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("pull_request", {"action": "opened"}),
        ("check_suite", {"action": "completed"}),
        ("workflow_run", {"action": "completed"}),
        ("status", {"state": "success"}),
    ],
)
def test_signed_future_event_remains_replayable_after_worker_sweep(
    event_name: str, payload: dict[str, object]
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    body = json.dumps(payload).encode()
    headers = _headers(body, delivery_id="future-1")
    headers["X-GitHub-Event"] = event_name
    status, response = _post(body, headers, uow)
    assert (status, response) == (202, {"status": "pending"})

    class DeferredDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=DeferredDispatcher(), now=lambda: now
    )

    assert asyncio.run(receiver.replay_pending()) == 1
    row = uow.rows["future-1"]
    assert row.projected is False
    assert row.claim_token is None
    assert row.retry_after == now + timedelta(minutes=5)
    assert row.delivery.event_name == event_name
    assert json.loads(row.delivery.payload_json) == payload


def test_unknown_pr_repository_receipt_remains_replayable_after_sweep() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    delivery = VerifiedGitHubDelivery("unknown-pr", "pull_request", {"action": "opened"})

    class UnknownRepositoryDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=UnknownRepositoryDispatcher(), now=lambda: now
    )

    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )
    assert asyncio.run(receiver.replay_pending()) == 1
    assert uow.rows["unknown-pr"].projected is False
    assert uow.rows["unknown-pr"].retry_after == now + timedelta(minutes=5)


def _outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("GitHub webhook delivery ")
    ]


def test_projected_delivery_logs_one_outcome_line_with_its_detail_and_no_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    payload = {"action": "labeled", "secret": "ghs_payload_sentinel"}
    receipts = [
        VerifiedGitHubDelivery("outcome-1", "pull_request", payload).to_receipt(),
        VerifiedGitHubDelivery("outcome-2", "installation", {"action": "created"}).to_receipt(),
    ]

    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            if delivery.delivery_id == "outcome-1":
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.PROJECTED_PR,
                    "pr=p head=aaaaaaa: ineligible (ci_blocked)",
                )
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=Dispatcher(), now=lambda: now
    )
    for receipt in receipts:
        asyncio.run(receiver.execute(receipt))

    with caplog.at_level(logging.INFO):
        assert asyncio.run(receiver.replay_pending()) == 2

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery outcome-1 event=pull_request status=projected_pr "
        "detail=pr=p head=aaaaaaa: ineligible (ci_blocked)",
        "GitHub webhook delivery outcome-2 event=installation status=onboarded detail=-",
    ]
    assert "ghs_payload_sentinel" not in caplog.text


def test_deferred_delivery_logs_when_it_is_tried_again_and_then_that_it_is_final(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    delivery = VerifiedGitHubDelivery("deferred-1", "pull_request", {"action": "labeled"})

    class UnknownRepositoryDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY, "unknown_repository"
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=UnknownRepositoryDispatcher(), now=lambda: now
    )
    asyncio.run(receiver.execute(delivery.to_receipt()))

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert asyncio.run(receiver.replay_pending()) == 1
            now += timedelta(minutes=6)

    prefix = (
        "GitHub webhook delivery deferred-1 event=pull_request "
        "status=ignored_unknown_repository detail=unknown_repository"
    )
    assert _outcome_lines(caplog) == [
        f"{prefix} retry_at=2026-09-28T00:05:00+00:00",
        f"{prefix} retry_at=2026-09-28T00:11:00+00:00",
        f"{prefix} retry_at=none",
    ]
    assert [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ] == [
        "GitHub webhook delivery deferred-1 deferred after its last attempt: "
        "ignored_unknown_repository"
    ]


def test_commit_failure_cannot_acknowledge_receipt() -> None:
    body = b'{"action":"opened"}'

    @dataclass
    class FailingCommitUnitOfWork(FakeReceiptUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("database commit failed")

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self.rows.clear()

    uow = FailingCommitUnitOfWork()
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: lambda: uow
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/webhooks/github", content=body, headers=_headers(body)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert uow.rows == {}


def test_worker_claim_commit_finishes_before_dispatch() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    delivery = VerifiedGitHubDelivery("ordered-1", "ping", {"zen": "hello"})

    class AssertingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            assert uow.commits == 2
            assert uow.rows["ordered-1"].claim_token is not None
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=AssertingDispatcher(), now=lambda: now
    )

    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )
    assert asyncio.run(receiver.replay_pending()) == 1
    assert uow.commits == 3


def _headers(raw_body: bytes, *, delivery_id: str = "delivery-42") -> dict[str, str]:
    digest = hmac.new(_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": "installation_repositories",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def _post(
    raw_body: bytes,
    headers: Mapping[str, str],
    receipts: FakeReceiptUnitOfWork,
) -> tuple[int, dict[str, str]]:
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: lambda: receipts
    try:
        response = TestClient(app).post("/webhooks/github", content=raw_body, headers=headers)
        return response.status_code, response.json()
    finally:
        app.dependency_overrides.clear()


def test_signed_delivery_is_saved_once_and_http_never_dispatches() -> None:
    body = json.dumps({"action": "added", "installation": {"id": 17}}).encode()
    receipts = FakeReceiptUnitOfWork()

    first = _post(body, _headers(body), receipts)
    second = _post(body, _headers(body), receipts)

    assert first == (202, {"status": "pending"})
    assert second == (202, {"status": "duplicate"})
    assert (
        receipts.rows["delivery-42"].delivery
        == VerifiedGitHubDelivery(
            delivery_id="delivery-42",
            event_name="installation_repositories",
            payload={"action": "added", "installation": {"id": 17}},
        ).to_receipt()
    )
    assert receipts.rows["delivery-42"].projected is False


def test_invalid_signature_never_saves_or_dispatches() -> None:
    body = b'{"action":"added"}'
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    status, response = _post(body, headers, receipts)

    assert status == 401
    assert response == {"detail": "invalid GitHub webhook signature"}
    assert receipts.rows == {}


def test_missing_headers_and_malformed_signed_json_are_rejected() -> None:
    body = b"not json"
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body)

    malformed_status, _ = _post(body, headers, receipts)
    missing_headers = {key: value for key, value in headers.items() if key != "X-GitHub-Event"}
    missing_status, _ = _post(body, missing_headers, receipts)

    assert malformed_status == 400
    assert missing_status == 400
    assert receipts.rows == {}


@pytest.mark.parametrize(
    ("payload", "event_name", "delivery_id"),
    [
        ({"action": "added"}, "x" * 101, "valid-id"),
        ({"action": "added"}, "push", "x" * 256),
        ({"action": 42}, "push", "valid-id"),
        ({"action": "x" * 101}, "push", "valid-id"),
        ({"installation": {"id": True}}, "push", "valid-id"),
        ({"installation": {"id": 9223372036854775808}}, "push", "valid-id"),
        ({"installation": {"id": 0}}, "push", "valid-id"),
        ({"installation": "invalid"}, "push", "valid-id"),
    ],
)
def test_invalid_delivery_fields_do_not_store_or_dispatch(
    payload: dict[str, object], event_name: str, delivery_id: str
) -> None:
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body, delivery_id=delivery_id)
    headers["X-GitHub-Event"] = event_name

    status, _ = _post(body, headers, receipts)

    assert status == 400
    assert receipts.rows == {}


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_signed_json_is_rejected_before_storage(constant: str) -> None:
    body = f'{{"score":{constant}}}'.encode()
    receipts = FakeReceiptUnitOfWork()

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"x": "\x00"},
        {"outer": {"x": ["safe", "bad\x00"]}},
        {"outer": {"bad\x00": "value"}},
    ],
)
def test_signed_json_with_nul_string_or_key_is_rejected_before_storage(
    payload: dict[str, object],
) -> None:
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()
    assert b"\\u0000" in body

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"outer": {"value": ["safe", "bad\ud800"]}},
        {"outer": {"bad\udfff": "value"}},
    ],
)
def test_signed_json_with_unpaired_surrogate_is_rejected_before_storage(
    payload: dict[str, object],
) -> None:
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


def test_signed_json_with_paired_surrogate_preserves_emoji_payload() -> None:
    payload = {"outer": {"emoji\U0001f600": ["value\U0001f600"]}}
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()
    assert b"\\ud83d\\ude00" in body

    status, response = _post(body, _headers(body), receipts)

    assert status == 202
    assert response == {"status": "pending"}
    assert json.loads(receipts.rows["delivery-42"].delivery.payload_json) == payload


@pytest.mark.parametrize("depth", [300, 10_000])
def test_excessively_nested_signed_json_is_rejected_before_storage(depth: int) -> None:
    body = b'{"nested":' * depth + b"0" + b"}" * depth
    receipts = FakeReceiptUnitOfWork()

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


@pytest.mark.parametrize("digest", ["z" * 64, "a" * 63])
def test_malformed_signature_is_rejected(digest: str) -> None:
    body = b"{}"
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body)
    headers["X-Hub-Signature-256"] = f"sha256={digest}"

    status, _ = _post(body, headers, receipts)

    assert status == 401
    assert receipts.rows == {}


def test_non_ascii_signature_is_rejected_by_verifier() -> None:
    assert not _has_valid_github_signature(
        raw_body=b"{}", signature=f"sha256={'é' * 64}", secret=_SECRET
    )


@pytest.mark.integration
def test_postgresql_claim_is_exclusive_expires_and_excludes_projected_rows(
    isolated_webhook_database: tuple[Connection, str, str],
) -> None:
    connection, database_url, schema = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        delivery = VerifiedGitHubDelivery("claim-1", "pull_request", {"action": "opened"})
        now = datetime(2026, 9, 28, tzinfo=UTC)
        first_token, second_token, replacement_token = uuid4(), uuid4(), uuid4()

        async def claim(token: UUID, at: datetime) -> WebhookReceipt | None:
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                claimed = await uow.receipts.claim(
                    delivery.delivery_id, token, at, at + timedelta(minutes=5)
                )
                if claimed is not None:
                    await uow.commit()
                return claimed

        try:
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.save(delivery.to_receipt()) is True
                await uow.commit()

            first, second = await asyncio.gather(claim(first_token, now), claim(second_token, now))
            assert (first is None) != (second is None)
            assert first == delivery.to_receipt() or second == delivery.to_receipt()
            winning_token = first_token if first is not None else second_token
            assert await claim(replacement_token, now + timedelta(minutes=4)) is None

            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(now + timedelta(minutes=4), 10) == ()

            assert (
                await claim(replacement_token, now + timedelta(minutes=6)) == delivery.to_receipt()
            )
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await uow.receipts.mark_projected(
                        delivery.delivery_id, winning_token, now + timedelta(minutes=6)
                    )

            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                await uow.receipts.mark_projected(
                    delivery.delivery_id, replacement_token, now + timedelta(minutes=6)
                )
                await uow.commit()

            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(now + timedelta(minutes=7), 10) == ()
            assert await claim(uuid4(), now + timedelta(minutes=7)) is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgresql_failed_dispatches_are_terminal_after_three_attempts(
    isolated_webhook_database: tuple[Connection, str, str],
) -> None:
    connection, database_url, schema = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        delivery = VerifiedGitHubDelivery("failure-limit-1", "ping", {"zen": "hello"})
        now = datetime(2026, 9, 28, tzinfo=UTC)
        try:
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.save(delivery.to_receipt()) is True
                await uow.commit()

            for attempt in range(1, 4):
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    token = uuid4()
                    assert (
                        await uow.receipts.claim(
                            delivery.delivery_id, token, now, now + timedelta(minutes=5)
                        )
                        == delivery.to_receipt()
                    )
                    await uow.receipts.release_after_dispatch_failure(
                        delivery.delivery_id,
                        token,
                        now + timedelta(seconds=30),
                        now,
                        max_attempts=3,
                    )
                    await uow.commit()

                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    expected_pending = (delivery.delivery_id,) if attempt < 3 else ()
                    assert await uow.receipts.pending_ids(now + timedelta(seconds=31), 10) == (
                        expected_pending
                    )
                now += timedelta(seconds=31)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_migration_preserves_legacy_receipt_and_concurrent_insert_is_unique(
    isolated_webhook_database: tuple[Connection, str, str],
) -> None:
    connection, database_url, schema = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20260925_0011")
    connection.execute(
        text(
            "INSERT INTO webhook_events "
            "(id, installation_external_id, delivery_id, event, action, "
            "payload_s3_ref) "
            "VALUES (:id, 17, 'legacy-1', 'installation', 'created', 's3://old')"
        ),
        {"id": uuid4()},
    )
    connection.commit()
    command.upgrade(config, "head")

    async def exercise() -> tuple[bool, bool]:
        async_engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        try:
            delivery = VerifiedGitHubDelivery(
                delivery_id="new-1",
                event_name="ping",
                payload={"zen": "hello", "nested": {"ok": True}},
            )

            async def save_once() -> bool:
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    inserted = await uow.receipts.save(delivery.to_receipt())
                    await uow.commit()
                    return inserted

            first, second = await asyncio.gather(save_once(), save_once())
            return first, second
        finally:
            await async_engine.dispose()

    assert sorted(asyncio.run(exercise())) == [False, True]
    rows = (
        connection.execute(
            text(
                "SELECT delivery_id, installation_external_id, payload, payload_s3_ref "
                "FROM webhook_events ORDER BY delivery_id"
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 2
    assert rows[0]["delivery_id"] == "legacy-1"
    assert rows[0]["payload_s3_ref"] == "s3://old"
    assert rows[0]["payload"] is None
    assert rows[1]["delivery_id"] == "new-1"
    assert rows[1]["installation_external_id"] is None
    assert rows[1]["payload"] == {"zen": "hello", "nested": {"ok": True}}
    assert rows[1]["payload_s3_ref"] is None


@pytest.mark.integration
def test_downgrade_preserves_legacy_rows_and_refuses_jsonb_data_loss(
    isolated_webhook_database: tuple[Connection, str, str],
) -> None:
    connection, _, _ = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20260925_0011")
    connection.execute(
        text(
            "INSERT INTO webhook_events "
            "(id, installation_external_id, delivery_id, event, payload_s3_ref) "
            "VALUES (:id, 17, 'legacy-1', 'installation', 's3://old')"
        ),
        {"id": uuid4()},
    )
    connection.commit()

    command.upgrade(config, "head")
    command.downgrade(config, "20260925_0011")
    assert (
        connection.scalar(
            text("SELECT payload_s3_ref FROM webhook_events WHERE delivery_id = 'legacy-1'")
        )
        == "s3://old"
    )
    connection.rollback()

    command.upgrade(config, "head")
    connection.execute(
        text(
            "INSERT INTO webhook_events (id, delivery_id, event, payload) "
            "VALUES (:id, 'new-1', 'ping', CAST('{\"zen\":\"hello\"}' AS JSONB))"
        ),
        {"id": uuid4()},
    )
    connection.commit()
    with pytest.raises(DatabaseError, match="Cannot downgrade webhook payloads"):
        command.downgrade(config, "20260925_0011")


@pytest.fixture
def isolated_webhook_database() -> Iterator[tuple[Connection, str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_webhook_receipts_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection, database_url, schema
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()
