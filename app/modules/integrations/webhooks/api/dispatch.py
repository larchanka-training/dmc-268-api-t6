"""Convert claimed raw GitHub receipts to validated application dispatch values."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Protocol

from pydantic import ValidationError

from app.common.application.log_token import log_token
from app.modules.integrations.webhooks.api.ci_event_dtos import parse_ci_event
from app.modules.integrations.webhooks.api.installation_event_dtos import (
    InstallationEventValidationError,
    UnsupportedInstallationAction,
    parse_installation_repositories_event,
)
from app.modules.integrations.webhooks.api.pull_request_dtos import (
    SUPPORTED_PULL_REQUEST_ACTIONS,
    PullRequestPayloadValidationError,
    parse_pull_request_event,
    parse_pull_request_label_event,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    UnsupportedGitHubEvent,
    _with_action,
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
# The outcome-line reason for a payload that cannot be parsed into its event's shape. Never a
# field value or an exception message; a schema error adds the failing field paths as
# ``fields=`` (docs/WEBHOOK_WORKER.md, outcome log).
_INVALID_PAYLOAD = "invalid_payload"
_MAX_LOGGED_FIELD_ERRORS = 10
# A field path segment goes into the outcome line only when it is an identifier.
_FIELD_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_LOGGER = logging.getLogger(__name__)
# The action goes into log lines, so it follows the same plain-token rule as the event name;
# anything else (free text, line breaks) is treated as absent.
_action_token = log_token


def action_of(receipt: WebhookReceipt) -> str | None:
    """The payload's action as a safe log token, or None when it has none.

    A payload that cannot be decoded is an error, not a missing action: it raises, and the
    failure line records that fallback (docs/WEBHOOK_WORKER.md, failure log).
    """
    decoded = json.loads(receipt.payload_json)
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
                InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT, _INVALID_PAYLOAD
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
                except (ValueError, ValidationError) as error:
                    return InstallationDeliveryDispatchResult(
                        InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT,
                        _with_action(action, _invalid_payload(error)),
                    )
        elif event_name == "status" or (event_name in _CI_EVENTS and action == "completed"):
            try:
                value = parse_ci_event(event_name, payload)
            except (ValueError, ValidationError) as error:
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT,
                    _invalid_payload(error),
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
                    InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT, _INVALID_PAYLOAD
                )
        else:
            value = UnsupportedGitHubEvent(event_name, action)
        return await self._dispatcher.execute(GitHubDispatchEvent(delivery.delivery_id, value))


def _invalid_payload(error: ValueError) -> str:
    """``invalid_payload``, followed by ``fields=<path>,...`` when the error names its fields.

    Paths only, never a value or a message: a pydantic error contributes its locations, the
    pull request rule error its fields; any other ``ValueError`` names none. Each path is
    logged once, the first ``_MAX_LOGGED_FIELD_ERRORS`` of them, then ``+<n>`` for the rest.
    """
    locations: list[tuple[int | str, ...]]
    if isinstance(error, ValidationError):
        locations = [
            tuple(item["loc"]) for item in error.errors(include_input=False, include_url=False)
        ]
    elif isinstance(error, PullRequestPayloadValidationError):
        locations = [(name,) for name in error.fields]
    else:
        return _INVALID_PAYLOAD
    paths = list(dict.fromkeys(_field_path(loc) for loc in locations))
    shown = paths[:_MAX_LOGGED_FIELD_ERRORS]
    omitted = len(paths) - len(shown)
    return f"{_INVALID_PAYLOAD} fields={','.join(shown)}{f',+{omitted}' if omitted else ''}"


def _field_path(loc: tuple[int | str, ...]) -> str:
    """The location's segments joined by ``.``; an empty location (a model error) is ``?``."""
    if not loc:
        return "?"
    return ".".join(_field_segment(part) for part in loc)


def _field_segment(part: int | str) -> str:
    """A list index as its number, a field name only when it is an identifier, else ``?``."""
    if isinstance(part, int):
        return str(part)
    return part if _FIELD_SEGMENT.fullmatch(part) else "?"


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
