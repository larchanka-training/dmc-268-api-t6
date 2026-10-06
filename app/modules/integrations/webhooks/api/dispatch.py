"""Convert claimed raw GitHub receipts to validated application dispatch values."""

from __future__ import annotations

import json
import logging
import re
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
_MAX_LOGGED_FIELD_ERRORS = 10
_LOGGER = logging.getLogger(__name__)
# GitHub's actions are lowercase words joined by ``_``. The action goes into log lines, so
# anything else (free text, line breaks) is treated as absent.
_ACTION_TOKEN = re.compile(r"[a-z_]{1,40}")


def _action_token(value: object) -> str | None:
    return value if isinstance(value, str) and _ACTION_TOKEN.fullmatch(value) else None


def action_of(receipt: WebhookReceipt) -> str | None:
    """The payload's action as a safe log token, or None; never raises."""
    try:
        decoded = json.loads(receipt.payload_json)
    except (TypeError, ValueError, RecursionError):
        return None
    return _action_token(decoded.get("action")) if isinstance(decoded, dict) else None


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
        action = _action_token(payload.get("action"))
        event_name = delivery.event_name
        value: (
            PullRequestEvent
            | PullRequestLabelEvent
            | CiTriggerEvent
            | InstallationRepositoriesEvent
            | UnsupportedGitHubEvent
        )
        if event_name == "pull_request":
            if action not in SUPPORTED_PULL_REQUEST_ACTIONS:
                value = UnsupportedGitHubEvent(event_name, action)
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
                value = UnsupportedGitHubEvent(event_name, _action_token(unsupported.action))
            except InstallationEventValidationError as invalid:
                _log_invalid_installation_event(delivery.delivery_id, event_name, payload, invalid)
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
                )
        else:
            value = UnsupportedGitHubEvent(event_name, action)
        return await self._dispatcher.execute(GitHubDispatchEvent(delivery.delivery_id, value))


def _log_invalid_installation_event(
    delivery_id: str,
    event_name: str,
    payload: Mapping[str, object],
    error: InstallationEventValidationError,
) -> None:
    """Say why a receipt was ignored without echoing any payload value.

    The receipt is still marked projected and never replayed, so this line is the
    only trace of the rejection: delivery, event, action, installation, the total
    number of failing fields and the first few locations with their messages (a
    signed event may list hundreds of repositories that all fail alike).
    """
    action = payload.get("action")
    installation = payload.get("installation")
    installation_id = installation.get("id") if isinstance(installation, Mapping) else None
    shown = error.field_errors[:_MAX_LOGGED_FIELD_ERRORS]
    omitted = len(error.field_errors) - len(shown)
    _LOGGER.warning(
        "Ignoring invalid GitHub installation event: delivery_id=%s event=%s action=%s "
        "installation_id=%s error_count=%d errors=%s%s",
        delivery_id,
        event_name,
        action if isinstance(action, str) else None,
        installation_id if isinstance(installation_id, int) else None,
        len(error.field_errors),
        "; ".join(f"{location}: {message}" for location, message in shown),
        f" (+{omitted} more)" if omitted else "",
    )
