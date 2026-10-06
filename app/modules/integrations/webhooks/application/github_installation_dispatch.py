"""Dispatch verified GitHub installation deliveries to repository onboarding."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    RepositoryDetailsUnavailableError,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestProjectionStatus,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent

_LOGGER = logging.getLogger(__name__)
_RUN_TRIGGER_PR_ACTIONS = frozenset({"reopened", "synchronize"})


def _with_action(action: str | None, detail: str | None) -> str | None:
    """Name the pull request action in the detail, so ``labeled`` differs from ``synchronize``."""
    if action is None:
        return detail
    return f"action={action}" if detail is None else f"action={action} {detail}"


class InstallationDeliveryDispatchStatus(StrEnum):
    """Observable result for a verified installation delivery."""

    ONBOARDED = "onboarded"
    IGNORED_INVALID_EVENT = "ignored_invalid_event"
    IGNORED_IRRELEVANT_EVENT = "ignored_irrelevant_event"
    IGNORED_UNKNOWN_INSTALLATION = "ignored_unknown_installation"
    DUPLICATE = "duplicate"
    PENDING = "pending"
    DEFERRED_KNOWN_EVENT = "deferred_known_event"
    PROJECTED_PR = "projected_pr"
    IGNORED_UNKNOWN_REPOSITORY = "ignored_unknown_repository"
    PROCESSED_CI = "processed_ci"
    DEFERRED_REPOSITORY_DETAILS = "deferred_repository_details"


@dataclass(frozen=True)
class InstallationDeliveryDispatchResult:
    """Result that lets the transport choose an acknowledgement response."""

    status: InstallationDeliveryDispatchStatus
    # Why the delivery ended as it did, prefixed with ``action=<action>`` for pull request
    # events (never a payload or a credential); logged by the worker.
    detail: str | None = None


@dataclass(frozen=True)
class UnsupportedGitHubEvent:
    """An event/action pair with no supported application behavior."""

    event_name: str
    action: str | None


@dataclass(frozen=True)
class GitHubDispatchEvent:
    """Validated application input from a claimed raw webhook receipt."""

    delivery_id: str
    value: (
        PullRequestEvent
        | PullRequestLabelEvent
        | CiTriggerEvent
        | InstallationRepositoriesEvent
        | UnsupportedGitHubEvent
    )


class GitHubInstallationResolver(Protocol):
    """Resolve only a pre-authorized GitHub installation to its internal id."""

    async def find_github_installation_id(self, external_id: int) -> UUID | None: ...


class InstallationOnboardingHandler(Protocol):
    """Application boundary that fetches trees before its short sync transaction."""

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        event: InstallationRepositoriesEvent,
    ) -> tuple[OnboardingResult, ...]: ...


class PullRequestProjectionHandler(Protocol):
    async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus: ...


class PullRequestLabelIntentHandler(Protocol):
    async def execute(self, event: PullRequestLabelEvent) -> PullRequestProjectionStatus: ...


class WebhookRunTriggerHandler(Protocol):
    """Each handler returns a short outcome line for the delivery log."""

    async def on_pr(self, event: PullRequestEvent) -> str | None: ...

    async def on_label(self, event: PullRequestLabelEvent) -> str | None: ...

    async def on_ci(self, event: CiTriggerEvent) -> str | None: ...


class GitHubInstallationDeliveryDispatcher:
    """Safely route supported GitHub installation events to onboarding.

    Validation happens before this boundary. If the installation has
    not previously been linked to a Workspace, the delivery is acknowledged as
    ignored and neither GitHub tree I/O nor database writes for onboarding run.
    """

    def __init__(
        self,
        *,
        resolver: GitHubInstallationResolver,
        onboarding: InstallationOnboardingHandler,
        pull_request_projector: PullRequestProjectionHandler | None = None,
        label_intent_projector: PullRequestLabelIntentHandler | None = None,
        run_trigger: WebhookRunTriggerHandler | None = None,
    ) -> None:
        self._resolver = resolver
        self._onboarding = onboarding
        self._pull_request_projector = pull_request_projector
        self._label_intent_projector = label_intent_projector
        self._run_trigger = run_trigger

    async def execute(self, delivery: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
        event = delivery.value
        if isinstance(event, UnsupportedGitHubEvent):
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
                detail=_with_action(event.action, None),
            )
        if isinstance(event, PullRequestLabelEvent):
            action = event.pull_request.action
            if event.label_name != "ai-review":
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
                    detail=_with_action(action, "label is not ai-review"),
                )
            if self._label_intent_projector is None:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
                    detail=_with_action(action, None),
                )
            projection = await self._label_intent_projector.execute(event)
            result = self._projection_result(projection, action)
            if (
                action == "labeled"
                and result.status == InstallationDeliveryDispatchStatus.PROJECTED_PR
            ):
                if self._run_trigger is None:
                    return InstallationDeliveryDispatchResult(
                        status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
                        detail=_with_action(action, None),
                    )
                detail = await self._run_trigger.on_label(event)
                return InstallationDeliveryDispatchResult(
                    result.status, _with_action(action, detail)
                )
            return result
        if isinstance(event, PullRequestEvent):
            if event.action in {"review_requested", "review_request_removed"}:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
                    detail=_with_action(event.action, None),
                )
            if self._pull_request_projector is None:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
                    detail=_with_action(event.action, None),
                )
            projection = await self._pull_request_projector.execute(event)
            result = self._projection_result(projection, event.action)
            if result.status != InstallationDeliveryDispatchStatus.PROJECTED_PR:
                return result
            if event.action in _RUN_TRIGGER_PR_ACTIONS:
                if self._run_trigger is None:
                    return InstallationDeliveryDispatchResult(
                        status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
                        detail=_with_action(event.action, None),
                    )
                detail = await self._run_trigger.on_pr(event)
                return InstallationDeliveryDispatchResult(
                    result.status, _with_action(event.action, detail)
                )
            return result
        if isinstance(event, CiTriggerEvent):
            if self._run_trigger is not None:
                detail = await self._run_trigger.on_ci(event)
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.PROCESSED_CI, detail=detail
                )
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
            )
        provider_installation_id = await self._resolver.find_github_installation_id(
            event.installation_external_id
        )
        if provider_installation_id is None:
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION
            )

        try:
            await self._onboarding.execute(
                provider_installation_id=provider_installation_id,
                event=event,
            )
        except (RepositoryDetailsUnavailableError, InstallationAccessTokenError) as error:
            # A token GitHub could not issue is a read it cannot answer either: same deferral.
            # A malformed token response or an unusable App key is permanent (api#71).
            if isinstance(error, InstallationAccessTokenError) and not error.transient:
                raise
            _LOGGER.warning(
                "Repository details unavailable for installation %s: %s",
                event.installation_external_id,
                error,
            )
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
            )
        return InstallationDeliveryDispatchResult(
            status=InstallationDeliveryDispatchStatus.ONBOARDED
        )

    @staticmethod
    def _projection_result(
        projection: PullRequestProjectionStatus, action: str
    ) -> InstallationDeliveryDispatchResult:
        # A disabled repository or one stored under another installation is deferred like an
        # unknown one; only the detail tells them apart.
        if projection in {
            PullRequestProjectionStatus.UNKNOWN_REPOSITORY,
            PullRequestProjectionStatus.DISABLED_REPOSITORY,
            PullRequestProjectionStatus.OTHER_INSTALLATION_REPOSITORY,
        }:
            status = InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY
        elif projection in {
            PullRequestProjectionStatus.PROJECTED,
            PullRequestProjectionStatus.IGNORED_STALE,
        }:
            status = InstallationDeliveryDispatchStatus.PROJECTED_PR
        else:
            status = InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
        return InstallationDeliveryDispatchResult(
            status=status, detail=_with_action(action, projection.value)
        )
