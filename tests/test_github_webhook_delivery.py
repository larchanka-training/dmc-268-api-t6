"""Verified GitHub webhook receipts are durable before projection."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application import receive_github_delivery as receipt_engine
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
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestProjectionStatus,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent, RunTriggerOutcome
from tests.github_webhook_fakes import (
    FakeClaimedReceipt,
    FakeDispatcher,
    FakeReceiptUnitOfWork,
    _headers,
    _post,
)


def test_receipt_defaults_to_github_and_hides_raw_payload_from_repr() -> None:
    receipt = WebhookReceipt("legacy-1", "ping", '{"secret":"payload-canary"}')

    assert receipt.provider == "github"
    assert receipt.origin_provider_installation_id is None
    assert "payload-canary" not in repr(receipt)


@pytest.mark.parametrize("provider", ["unknown-provider-canary", "", "GITHUB"])
def test_receipt_refuses_unknown_provider_without_echoing_input(provider: str) -> None:
    with pytest.raises(ValueError, match="^unsupported webhook provider$"):
        WebhookReceipt("invalid-1", "ping", "{}", provider=provider)


def test_gitlab_receipt_requires_an_immutable_origin_uuid() -> None:
    with pytest.raises(ValueError, match="^GitLab receipt requires an immutable origin UUID$"):
        WebhookReceipt("gitlab-1", "Merge Request Hook", "{}", provider="gitlab")

    origin = UUID("00000000-0000-0000-0000-000000000169")
    receipt = WebhookReceipt(
        "gitlab-1",
        "Merge Request Hook",
        "{}",
        provider="gitlab",
        origin_provider_installation_id=origin,
    )
    assert receipt.provider == "gitlab"
    assert receipt.origin_provider_installation_id == origin


def test_gitlab_receipt_refuses_a_non_uuid_origin_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="^GitLab receipt requires an immutable origin UUID$"):
        WebhookReceipt(
            "gitlab-invalid-origin",
            "Merge Request Hook",
            "{}",
            provider="gitlab",
            origin_provider_installation_id=cast(UUID, "origin-credential-canary"),
        )


def test_github_receiver_refuses_gitlab_before_saving_or_committing() -> None:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(uow_factory=lambda: uow)
    receipt = WebhookReceipt(
        "other-provider",
        "Merge Request Hook",
        "{}",
        provider="gitlab",
        origin_provider_installation_id=uuid4(),
    )
    with pytest.raises(ValueError, match="^webhook receipt provider does not match receiver$"):
        asyncio.run(receiver.execute(receipt))
    assert uow.commits == 0
    assert uow.rows == {}


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_shared_receipt_sweep_keeps_secret_exception_and_payload_out_of_logs(
    provider: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 10, 19, tzinfo=UTC)
    label = "GitHub" if provider == "github" else "GitLab"
    receipt = WebhookReceipt(
        "safe-log-1",
        "merge_request",
        '{"object_attributes":{"action":"open"},"secret":"raw-payload-canary"}',
        provider=provider,
        origin_provider_installation_id=UUID("00000000-0000-0000-0000-000000000169"),
    )
    uow = FakeReceiptUnitOfWork()

    class FailingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            try:
                raise ValueError("nested-credential-canary")
            except ValueError as cause:
                raise RuntimeError(f"credential-canary {delivery.payload_json}") from cause

    receiver = (
        ReceiveGitHubDelivery(
            uow_factory=lambda: uow, dispatcher=FailingDispatcher(), now=lambda: now
        )
        if provider == "github"
        else receipt_engine.ReceiveWebhookDelivery(
            provider="gitlab",
            uow_factory=lambda: uow,
            dispatcher=FailingDispatcher(),
            now=lambda: now,
        )
    )
    with caplog.at_level(logging.INFO):
        asyncio.run(receiver.execute(receipt))
        assert asyncio.run(receiver.replay_pending()) == 0
    assert "canary" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert (
        f"{label} webhook delivery safe-log-1 event=merge_request action=- "
        "failed stage=dispatch category=internal error=RuntimeError" in caplog.text
    )
    assert f"{label} webhook sweep: 0 handled" in caplog.text
    assert uow.rows["safe-log-1"].retry_after == now + timedelta(seconds=30)


def test_gitlab_shared_engine_uses_its_own_action_reader(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime(2026, 10, 19, tzinfo=UTC)
    receipt = WebhookReceipt(
        "gitlab-action-1",
        "Merge Request Hook",
        '{"object_attributes":{"action":"open"}}',
        provider="gitlab",
        origin_provider_installation_id=uuid4(),
    )
    uow = FakeReceiptUnitOfWork()

    class FailingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            raise TimeoutError("timeout-secret-canary")

    receiver = receipt_engine.ReceiveWebhookDelivery(
        provider="gitlab",
        uow_factory=lambda: uow,
        dispatcher=FailingDispatcher(),
        now=lambda: now,
        action_of=lambda delivery: cast(
            str, json.loads(delivery.payload_json)["object_attributes"]["action"]
        ),
    )
    with caplog.at_level(logging.INFO):
        asyncio.run(receiver.execute(receipt))
        assert asyncio.run(receiver.replay_pending()) == 0
    assert "canary" not in caplog.text
    assert "GitHub" not in caplog.text
    assert "action=open failed stage=dispatch category=timeout error=TimeoutError" in caplog.text


@pytest.mark.parametrize("phase", ["claim", "finalize", "release"])
def test_receipt_sweep_hides_other_boundary_exception_messages(
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 10, 19, tzinfo=UTC)

    class FailingBoundary(FakeReceiptUnitOfWork):
        async def claim(
            self, delivery_id: str, token: UUID, now: datetime, until: datetime
        ) -> WebhookReceipt | None:
            if phase == "claim":
                raise RuntimeError("claim-secret-canary")
            return await super().claim(delivery_id, token, now, until)

        async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
            raise RuntimeError("finalize-secret-canary")

        async def release_after_dispatch_failure(
            self,
            delivery_id: str,
            token: UUID,
            retry_after: datetime,
            failed_at: datetime,
            max_attempts: int,
        ) -> bool:
            raise RuntimeError("release-secret-canary")

    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            if phase == "release":
                raise RuntimeError("dispatch-secret-canary")
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    uow = FailingBoundary()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=Dispatcher(),
        now=lambda: now,
    )
    asyncio.run(receiver.execute(WebhookReceipt("boundary-1", "ping", "{}")))
    with caplog.at_level(logging.INFO):
        assert asyncio.run(receiver.replay_pending()) == 0
    assert "canary" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_bitbucket_push_continuation_retries_without_terminalizing_long_fanout() -> None:
    clock = [datetime(2026, 10, 9, tzinfo=UTC)]
    uow = FakeReceiptUnitOfWork()
    delivery = WebhookReceipt(
        "bitbucket-push-many",
        "repo:push",
        "{}",
        "bitbucket",
        UUID(int=1),
        UUID(int=2),
        1,
        2,
    )

    @dataclass
    class Dispatcher:
        calls: int = 0

        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            self.calls += 1
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.CONTINUING_PUSH
                if self.calls <= 100
                else InstallationDeliveryDispatchStatus.WAITING_PUSH_CANDIDATE
                if self.calls <= 103
                else InstallationDeliveryDispatchStatus.FAILED_PUSH_CANDIDATES
            )

    dispatcher = Dispatcher()
    receiver = receipt_engine.ReceiveWebhookDelivery(
        provider="bitbucket",
        uow_factory=lambda: uow,
        dispatcher=dispatcher,
        now=lambda: clock[0],
    )
    assert asyncio.run(receiver.execute(delivery)) == InstallationDeliveryDispatchStatus.PENDING
    uow.rows[delivery.delivery_id].received_at = clock[0] - timedelta(hours=2)
    for _ in range(100):
        assert asyncio.run(receiver.replay_pending()) == 1
        row = uow.rows[delivery.delivery_id]
        assert not row.projected
        assert row.projection_failed_at is None
        assert row.projection_deferred_at is None
        assert row.projection_attempt_count == 0
        clock[0] += timedelta(seconds=2)
    for _ in range(3):
        assert asyncio.run(receiver.replay_pending()) == 1
        row = uow.rows[delivery.delivery_id]
        assert not row.projected
        assert row.projection_failed_at is None
        assert row.projection_deferred_at is None
        assert row.projection_attempt_count == 0
        clock[0] += timedelta(seconds=31)
    assert asyncio.run(receiver.replay_pending()) == 1
    assert not uow.rows[delivery.delivery_id].projected
    assert uow.rows[delivery.delivery_id].projection_failed_at == clock[0]
    assert dispatcher.calls == 104


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
        async def on_label(self, event: PullRequestLabelEvent) -> RunTriggerOutcome:
            assert event.label_name == "ai-review"
            return RunTriggerOutcome("")

        async def on_pr(self, event: PullRequestEvent) -> RunTriggerOutcome:
            raise AssertionError("label should not use PR trigger")

        async def on_ci(self, event: CiTriggerEvent) -> RunTriggerOutcome:
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
        (
            PullRequestProjectionStatus.IGNORED_OWN_BOT,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            True,
        ),
        (
            PullRequestProjectionStatus.IGNORED_IDENTITY_MISMATCH,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            True,
        ),
        (
            PullRequestProjectionStatus.IGNORED_IDENTITY_CONFLICT,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            True,
        ),
        (
            PullRequestProjectionStatus.IGNORED_EXTERNAL_ID_MISMATCH,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            True,
        ),
        (
            PullRequestProjectionStatus.IGNORED_OTHER_LABEL,
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
