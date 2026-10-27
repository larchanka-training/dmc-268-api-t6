"""Claimed Bitbucket PR and branch-push events use their stored repository origin."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from uuid import UUID

from app.modules.integrations.bitbucket.api.dispatch import BitbucketWebhookDispatchAdapter
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus as Status,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult

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
    pushes: list[tuple[UUID, UUID, UUID, frozenset[str], int, int]] = field(default_factory=list)

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
    ) -> tuple[EnqueueResult, ...]:
        self.pushes.append(
            (
                installation_id,
                repository_id,
                repository_uuid,
                branches,
                binding_generation,
                webhook_generation,
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
        assert handler.pushes == [(INSTALLATION, REPOSITORY, REMOTE, frozenset({"feature"}), 2, 3)]

    asyncio.run(scenario())


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
