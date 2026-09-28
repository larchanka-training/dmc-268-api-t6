"""Dispatch verified GitHub installation deliveries to repository onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

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

_RUN_TRIGGER_PR_ACTIONS = frozenset({"reopened"})


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


@dataclass(frozen=True)
class InstallationDeliveryDispatchResult:
    """Result that lets the transport choose an acknowledgement response."""

    status: InstallationDeliveryDispatchStatus


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
    async def on_pr(self, event: PullRequestEvent) -> None: ...

    async def on_label(self, event: PullRequestLabelEvent) -> None: ...

    async def on_ci(self, event: CiTriggerEvent) -> None: ...


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
                status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
            )
        if isinstance(event, PullRequestLabelEvent):
            if event.label_name != "ai-review":
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
                )
            if self._label_intent_projector is None:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                )
            projection = await self._label_intent_projector.execute(event)
            result = self._projection_result(projection)
            if (
                event.pull_request.action == "labeled"
                and result.status == InstallationDeliveryDispatchStatus.PROJECTED_PR
            ):
                if self._run_trigger is None:
                    return InstallationDeliveryDispatchResult(
                        status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                    )
                await self._run_trigger.on_label(event)
            return result
        if isinstance(event, PullRequestEvent):
            if event.action in {"review_requested", "review_request_removed"}:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
                )
            if self._pull_request_projector is None:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                )
            projection = await self._pull_request_projector.execute(event)
            result = self._projection_result(projection)
            if result.status != InstallationDeliveryDispatchStatus.PROJECTED_PR:
                return result
            if event.action in _RUN_TRIGGER_PR_ACTIONS:
                if self._run_trigger is None:
                    return InstallationDeliveryDispatchResult(
                        status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                    )
                await self._run_trigger.on_pr(event)
            return result
        if isinstance(event, CiTriggerEvent):
            if self._run_trigger is not None:
                await self._run_trigger.on_ci(event)
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.PROCESSED_CI
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

        await self._onboarding.execute(
            provider_installation_id=provider_installation_id,
            event=event,
        )
        return InstallationDeliveryDispatchResult(
            status=InstallationDeliveryDispatchStatus.ONBOARDED
        )

    @staticmethod
    def _projection_result(
        projection: PullRequestProjectionStatus,
    ) -> InstallationDeliveryDispatchResult:
        if projection == PullRequestProjectionStatus.UNKNOWN_REPOSITORY:
            status = InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY
        elif projection in {
            PullRequestProjectionStatus.PROJECTED,
            PullRequestProjectionStatus.IGNORED_STALE,
        }:
            status = InstallationDeliveryDispatchStatus.PROJECTED_PR
        else:
            status = InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
        return InstallationDeliveryDispatchResult(status=status)
