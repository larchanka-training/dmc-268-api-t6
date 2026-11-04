"""Claimed Bitbucket PR and branch-push events use their stored repository origin."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from app.modules.integrations.bitbucket.api.dispatch import BitbucketWebhookDispatchAdapter
from app.modules.integrations.bitbucket.application.provider_error import BitbucketProviderError
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus as Status,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    GitHubWebhookReceiptUnitOfWork,
    ReceiveWebhookDelivery,
    WebhookDispatchRetry,
    WebhookReceipt,
)
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestUntrackedClosed,
    BitbucketPushCandidatesFailed,
    BitbucketPushContinuationPending,
    BitbucketPushRetryPending,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult, EnqueueStatus

INSTALLATION = UUID(int=31)
REPOSITORY = UUID(int=32)
REMOTE = UUID(int=33)


def receipt(event: str, payload: dict[str, object]) -> WebhookReceipt:
    return WebhookReceipt(
        "bitbucket:" + str(INSTALLATION) + ":" + str(REPOSITORY) + ":" + "a" * 64,
        event,
        json.dumps({"repository": {"uuid": "{" + str(REMOTE) + "}"}, **payload}),
        "bitbucket",
        INSTALLATION,
        REPOSITORY,
        2,
        3,
    )


@dataclass
class Handler:
    pull_requests: list[tuple[UUID, UUID, UUID, int, int, int]] = field(default_factory=list)
    pushes: list[tuple[UUID, UUID, UUID, frozenset[str], int, int, str]] = field(
        default_factory=list
    )

    async def pull_request(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        number: int,
        binding_generation: int,
        webhook_generation: int,
    ) -> EnqueueResult | None:
        self.pull_requests.append(
            (
                installation_id,
                repository_id,
                repository_uuid,
                number,
                binding_generation,
                webhook_generation,
            )
        )
        return None

    async def push(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
        delivery_id: str,
    ) -> tuple[EnqueueResult, ...]:
        self.pushes.append(
            (
                installation_id,
                repository_id,
                repository_uuid,
                branches,
                binding_generation,
                webhook_generation,
                delivery_id,
            )
        )
        return ()


def test_dispatches_pr_and_relevant_branch_push_with_bound_origin() -> None:
    async def scenario() -> None:
        handler = Handler()
        adapter = BitbucketWebhookDispatchAdapter(handler)
        pr = await adapter.execute(receipt("pullrequest:created", {"pullrequest": {"id": 7}}))
        assert pr.status == Status.PROJECTED_PR
        assert handler.pull_requests == [(INSTALLATION, REPOSITORY, REMOTE, 7, 2, 3)]
        push = await adapter.execute(
            receipt(
                "repo:push",
                {
                    "push": {
                        "changes": [
                            {
                                "new": {
                                    "type": "branch",
                                    "name": "feature",
                                    "target": {"hash": "a" * 40},
                                }
                            },
                            {"new": {"type": "tag", "name": "v1"}},
                            {"new": None},
                        ]
                    }
                },
            )
        )
        assert push.status == Status.PROJECTED_PR
        assert handler.pushes == [
            (
                INSTALLATION,
                REPOSITORY,
                REMOTE,
                frozenset({"feature"}),
                2,
                3,
                receipt("repo:push", {}).delivery_id,
            )
        ]

    asyncio.run(scenario())


def test_push_page_requests_another_worker_pass() -> None:
    class ContinuingHandler(Handler):
        async def push(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            branches: frozenset[str],
            binding_generation: int,
            webhook_generation: int,
            delivery_id: str,
        ) -> tuple[EnqueueResult, ...]:
            raise BitbucketPushContinuationPending()

    result = asyncio.run(
        BitbucketWebhookDispatchAdapter(ContinuingHandler()).execute(
            receipt("repo:push", {"push": {"changes": [{"new": {"type": "branch", "name": "x"}}]}})
        )
    )
    assert result.status == Status.CONTINUING_PUSH

    class WaitingHandler(ContinuingHandler):
        async def push(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            branches: frozenset[str],
            binding_generation: int,
            webhook_generation: int,
            delivery_id: str,
        ) -> tuple[EnqueueResult, ...]:
            raise BitbucketPushRetryPending()

    waiting = asyncio.run(
        BitbucketWebhookDispatchAdapter(WaitingHandler()).execute(
            receipt("repo:push", {"push": {"changes": [{"new": {"type": "branch", "name": "x"}}]}})
        )
    )
    assert waiting.status == Status.WAITING_PUSH_CANDIDATE

    class FailedHandler(ContinuingHandler):
        async def push(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            branches: frozenset[str],
            binding_generation: int,
            webhook_generation: int,
            delivery_id: str,
        ) -> tuple[EnqueueResult, ...]:
            raise BitbucketPushCandidatesFailed()

    failed = asyncio.run(
        BitbucketWebhookDispatchAdapter(FailedHandler()).execute(
            receipt("repo:push", {"push": {"changes": [{"new": {"type": "branch", "name": "x"}}]}})
        )
    )
    assert failed.status == Status.FAILED_PUSH_CANDIDATES


def test_dispatch_rejects_invalid_identity_and_ignores_unrelated_event() -> None:
    async def scenario() -> None:
        handler = Handler()
        adapter = BitbucketWebhookDispatchAdapter(handler)
        bad = await adapter.execute(receipt("pullrequest:updated", {"pullrequest": {"id": True}}))
        assert bad.status == Status.IGNORED_INVALID_EVENT
        other = await adapter.execute(receipt("repo:commit_comment_created", {}))
        assert other.status == Status.IGNORED_IRRELEVANT_EVENT
        assert handler.pull_requests == [] and handler.pushes == []

    asyncio.run(scenario())


def test_token_rotation_defers_receipt_but_binding_change_is_inactive() -> None:
    class RotatingHandler(Handler):
        failure: Exception | None = None

        async def pull_request(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            number: int,
            binding_generation: int,
            webhook_generation: int,
        ) -> EnqueueResult | None:
            if self.failure is not None:
                raise self.failure
            return EnqueueResult(EnqueueStatus.STALE, reason="state_changed")

    async def scenario() -> None:
        handler = RotatingHandler()
        adapter = BitbucketWebhookDispatchAdapter(handler)
        delivery = receipt("pullrequest:updated", {"pullrequest": {"id": 7}})
        handler.failure = BitbucketCredentialGenerationChanged()
        rotated = await adapter.execute(delivery)
        assert rotated.status == Status.DEFERRED_KNOWN_EVENT
        handler.failure = BitbucketBindingGenerationChanged()
        rebound = await adapter.execute(delivery)
        assert rebound.status == Status.IGNORED_IRRELEVANT_EVENT
        assert rebound.detail == "binding_generation_changed"
        handler.failure = BitbucketPullRequestUntrackedClosed()
        untracked_close = await adapter.execute(delivery)
        assert untracked_close.status == Status.IGNORED_IRRELEVANT_EVENT
        assert untracked_close.detail == "untracked_closed_pr"
        handler.failure = None
        stale = await adapter.execute(delivery)
        assert stale.status == Status.DEFERRED_KNOWN_EVENT

    asyncio.run(scenario())


def test_push_rate_limit_preserves_provider_retry_delay() -> None:
    class RateLimitedHandler(Handler):
        async def push(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            branches: frozenset[str],
            binding_generation: int,
            webhook_generation: int,
            delivery_id: str,
        ) -> tuple[EnqueueResult, ...]:
            raise BitbucketProviderError("provider_rate_limited", retry_after=3600)

    async def scenario() -> None:
        with pytest.raises(WebhookDispatchRetry) as error:
            await BitbucketWebhookDispatchAdapter(RateLimitedHandler()).execute(
                receipt(
                    "repo:push",
                    {"push": {"changes": [{"new": {"type": "branch", "name": "feature"}}]}},
                )
            )
        assert error.value.delay_seconds == 3600

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("dispatch_result", "expected_delay"),
    [
        (WebhookDispatchRetry(3600), timedelta(hours=1)),
        (InstallationDeliveryDispatchResult(Status.CONTINUING_PUSH), timedelta(0)),
    ],
)
def test_push_retry_keeps_receipt_attempts_and_due_time(
    dispatch_result: WebhookDispatchRetry | InstallationDeliveryDispatchResult,
    expected_delay: timedelta,
) -> None:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    delivery = receipt(
        "repo:push", {"push": {"changes": [{"new": {"type": "branch", "name": "feature"}}]}}
    )

    class ReceiptStore:
        retry_at: datetime | None = None
        failure_releases = 0

        async def pending_ids(self, at: datetime, limit: int) -> tuple[str, ...]:
            assert at == now and limit == 1
            return (delivery.delivery_id,)

        async def claim(
            self, delivery_id: str, token: UUID, at: datetime, until: datetime
        ) -> WebhookReceipt:
            assert delivery_id == delivery.delivery_id and at == now
            assert until == now + timedelta(minutes=5)
            return delivery

        async def retry_push_continuation(
            self, delivery_id: str, token: UUID, retry_after: datetime
        ) -> None:
            assert delivery_id == delivery.delivery_id
            self.retry_at = retry_after

        async def release_after_dispatch_failure(self, *_: object) -> bool:
            self.failure_releases += 1
            return False

    class Uow:
        def __init__(self, receipts: ReceiptStore) -> None:
            self.receipts = receipts

        async def __aenter__(self) -> Uow:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def commit(self) -> None:
            pass

    class Dispatcher:
        async def execute(self, claimed: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            assert claimed == delivery
            if isinstance(dispatch_result, WebhookDispatchRetry):
                raise dispatch_result
            return dispatch_result

    async def scenario() -> None:
        store = ReceiptStore()
        receiver = ReceiveWebhookDelivery(
            provider="bitbucket",
            uow_factory=lambda: cast(GitHubWebhookReceiptUnitOfWork, Uow(store)),
            dispatcher=Dispatcher(),
            now=lambda: now,
        )
        result = await receiver.replay_batch(limit=1)
        assert (result.handled_count, result.error_count) == (1, 0)
        assert store.retry_at == now + expected_delay
        assert store.failure_releases == 0

    asyncio.run(scenario())
