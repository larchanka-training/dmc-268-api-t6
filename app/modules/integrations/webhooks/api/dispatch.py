"""Convert claimed raw GitHub receipts to validated application dispatch values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from pydantic import ValidationError

from app.modules.integrations.webhooks.api.ci_event_dtos import parse_ci_event
from app.modules.integrations.webhooks.api.installation_event_dtos import (
    InstallationEventValidationError,
    UnsupportedInstallationAction,
    parse_installation_repositories_event,
)
from app.modules.integrations.webhooks.api.pull_request_dtos import (
    SUPPORTED_PULL_REQUEST_ACTIONS,
    parse_pull_request_event,
    parse_pull_request_label_event,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    UnsupportedGitHubEvent,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
)
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent

_CI_EVENTS = frozenset({"check_suite", "workflow_run"})


class TypedGitHubDeliveryDispatcher(Protocol):
    async def execute(
        self, delivery: GitHubDispatchEvent
    ) -> InstallationDeliveryDispatchResult: ...


class GitHubWebhookDispatchAdapter:
    """Parse after receipt claim, then pass only typed values to the application."""

    def __init__(self, dispatcher: TypedGitHubDeliveryDispatcher) -> None:
        self._dispatcher = dispatcher

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        try:
            decoded = json.loads(delivery.payload_json)
        except (TypeError, ValueError):
            decoded = None
        if not isinstance(decoded, dict):
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
            )
        payload: Mapping[str, object] = decoded
        action = payload.get("action")
        event_name = delivery.event_name
        value: (
            PullRequestEvent
            | PullRequestLabelEvent
            | CiTriggerEvent
            | InstallationRepositoriesEvent
            | UnsupportedGitHubEvent
        )
        if event_name == "pull_request":
            if not isinstance(action, str) or action not in SUPPORTED_PULL_REQUEST_ACTIONS:
                value = UnsupportedGitHubEvent(
                    event_name, action if isinstance(action, str) else None
                )
            else:
                try:
                    value = (
                        parse_pull_request_label_event(payload)
                        if action in {"labeled", "unlabeled"}
                        else parse_pull_request_event(payload)
                    )
                except (ValueError, ValidationError):
                    return InstallationDeliveryDispatchResult(
                        InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
                    )
        elif event_name == "status" or (event_name in _CI_EVENTS and action == "completed"):
            try:
                value = parse_ci_event(event_name, payload)
            except (ValueError, ValidationError):
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
                )
        elif event_name in {"installation", "installation_repositories"}:
            try:
                value = parse_installation_repositories_event(
                    event_name=event_name, payload=payload
                )
            except UnsupportedInstallationAction as unsupported:
                value = UnsupportedGitHubEvent(event_name, unsupported.action)
            except InstallationEventValidationError:
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
                )
        else:
            value = UnsupportedGitHubEvent(event_name, action if isinstance(action, str) else None)
        return await self._dispatcher.execute(GitHubDispatchEvent(delivery.delivery_id, value))
