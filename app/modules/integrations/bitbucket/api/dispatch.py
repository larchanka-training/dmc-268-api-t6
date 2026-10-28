"""Decode claimed Bitbucket receipts; their signed origin remains authoritative."""

from __future__ import annotations

import json
from typing import Protocol, cast
from uuid import UUID

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketTargetInactive,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestIdentityMismatch,
    BitbucketPullRequestStale,
    BitbucketPullRequestUntrackedClosed,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult, EnqueueStatus


class BitbucketWebhookHandler(Protocol):
    async def pull_request(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        number: int,
        binding_generation: int,
        webhook_generation: int,
    ) -> EnqueueResult | None: ...

    async def push(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> tuple[EnqueueResult, ...]: ...


class BitbucketWebhookDispatchAdapter:
    def __init__(self, handler: BitbucketWebhookHandler) -> None:
        self._handler = handler

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        if delivery.event_name not in {
            "pullrequest:created",
            "pullrequest:updated",
            "pullrequest:fulfilled",
            "pullrequest:rejected",
            "repo:push",
        }:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
            )
        try:
            installation = delivery.origin_provider_installation_id
            repository = delivery.origin_repository_id
            binding = delivery.origin_binding_generation
            webhook = delivery.origin_webhook_generation
            if (
                delivery.provider != "bitbucket"
                or not isinstance(installation, UUID)
                or not isinstance(repository, UUID)
                or type(binding) is not int
                or type(webhook) is not int
                or binding <= 0
                or webhook <= 0
            ):
                raise ValueError("invalid_origin")
            payload = json.loads(delivery.payload_json)
            if not isinstance(payload, dict):
                raise ValueError("invalid_payload")
            repo = payload.get("repository")
            if not isinstance(repo, dict) or not isinstance(repo.get("uuid"), str):
                raise ValueError("invalid_repository")
            repository_uuid = UUID(repo["uuid"].strip("{}"))
            if delivery.event_name == "repo:push":
                branches = _push_branches(payload)
            else:
                pr = payload.get("pullrequest")
                number = pr.get("id") if isinstance(pr, dict) else None
                if type(number) is not int or not 0 < number <= 2147483647:
                    raise ValueError("invalid_pr")
        except (ValueError, TypeError, RecursionError):
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT, "invalid_payload"
            )
        if delivery.event_name == "repo:push" and not branches:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
            )
        try:
            if delivery.event_name == "repo:push":
                outcomes = await self._handler.push(
                    installation, repository, repository_uuid, branches, binding, webhook
                )
            else:
                outcome = await self._handler.pull_request(
                    installation, repository, repository_uuid, cast(int, number), binding, webhook
                )
                outcomes = (outcome,) if outcome is not None else ()
        except (BitbucketTargetInactive, BitbucketBindingGenerationChanged):
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT, "inactive_origin"
            )
        except BitbucketCredentialGenerationChanged:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT, "credential_rotated"
            )
        except BitbucketPullRequestStale:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT, "canonical_stale"
            )
        except BitbucketPullRequestUntrackedClosed:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT, "untracked_closed_pr"
            )
        except BitbucketPullRequestIdentityMismatch:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT, "identity_mismatch"
            )
        if any(outcome.status == EnqueueStatus.DEFERRED for outcome in outcomes):
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER,
                EnqueueStatus.DEFERRED.value,
            )
        if any(outcome.status == EnqueueStatus.STALE for outcome in outcomes):
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT, "canonical_stale"
            )
        return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.PROJECTED_PR)


def _push_branches(payload: dict[str, object]) -> frozenset[str]:
    push = payload.get("push")
    changes = push.get("changes") if isinstance(push, dict) else None
    if not isinstance(changes, list) or len(changes) > 1000:
        raise ValueError("invalid_push")
    branches: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("invalid_push")
        new = change.get("new")
        if new is None:
            continue
        if not isinstance(new, dict):
            raise ValueError("invalid_push")
        if new.get("type") != "branch":
            continue
        name = new.get("name")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 255
            or any(ord(char) < 32 for char in name)
        ):
            raise ValueError("invalid_push")
        branches.add(name)
    return frozenset(branches)
