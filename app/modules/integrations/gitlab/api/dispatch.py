"""Decode claimed GitLab receipts without deriving authority from webhook metadata."""

from __future__ import annotations

import json

from app.common.application.log_token import log_token
from app.modules.integrations.gitlab.api.dtos import parse_gitlab_payload
from app.modules.integrations.gitlab.application.request_credentials import GitLabTargetInactive
from app.modules.integrations.gitlab.application.webhook_receipts import (
    validate_gitlab_receipt_identity,
    validate_gitlab_receipt_payload,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.reviews.application.project_gitlab_merge_request import (
    CanonicalMergeRequestIdentityMismatch,
    CanonicalMergeRequestNotReady,
    CanonicalMergeRequestStale,
    GitLabMergeRequestAction,
    GitLabMergeRequestCommand,
    GitLabMergeRequestHandler,
    GitLabMergeRequestHandlingResult,
    GitLabMergeRequestIdentityConflict,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueStatus


def action_of(receipt: WebhookReceipt) -> str | None:
    decoded = json.loads(receipt.payload_json)
    attributes = decoded.get("object_attributes") if isinstance(decoded, dict) else None
    return log_token(attributes.get("action")) if isinstance(attributes, dict) else None


class GitLabWebhookDispatchAdapter:
    def __init__(self, handler: GitLabMergeRequestHandler) -> None:
        self._handler = handler

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        try:
            origin = validate_gitlab_receipt_identity(delivery)
            parsed = parse_gitlab_payload(
                delivery.payload_json.encode("utf-8"), event_name=delivery.event_name
            )
            project_id = validate_gitlab_receipt_payload(
                parsed.payload, event_name=delivery.event_name
            )
            if delivery.event_name != "Merge Request Hook":
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
                )
            mr = parsed.merge_request
            if mr is None or project_id is None:
                raise ValueError("invalid merge request identity")
            attributes = mr.object_attributes
            if attributes.action not in GitLabMergeRequestAction:
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
                )
            command = GitLabMergeRequestCommand(
                origin,
                project_id,
                attributes.id,
                attributes.iid,
                attributes.action,
            )
        except ValueError:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT, "invalid_payload"
            )
        try:
            result = await self._handler.execute(command)
        except GitLabTargetInactive:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT, "inactive_origin"
            )
        except CanonicalMergeRequestNotReady:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT, "canonical_not_ready"
            )
        except CanonicalMergeRequestStale:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT, "canonical_stale"
            )
        except CanonicalMergeRequestIdentityMismatch:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT, "identity_mismatch"
            )
        except GitLabMergeRequestIdentityConflict:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT, "identity_conflict"
            )
        # Generation/operational/upstream failures retain the receipt engine's
        # existing finite failure budget; programming errors and cancellation escape.
        if isinstance(result, GitLabMergeRequestHandlingResult) and result.enqueue is not None:
            status = (
                InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER
                if result.enqueue.status == EnqueueStatus.DEFERRED
                else InstallationDeliveryDispatchStatus.PROJECTED_PR
            )
            # Only the typed status is safe diagnostic data; arbitrary port details
            # and reasons may contain inputs or upstream exception text.
            return InstallationDeliveryDispatchResult(status, result.enqueue.status.value)
        return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.PROJECTED_PR)
