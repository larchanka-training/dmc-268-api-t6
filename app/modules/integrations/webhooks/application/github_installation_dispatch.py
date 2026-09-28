"""Dispatch verified GitHub installation deliveries to repository onboarding."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.repositories.application.installation_repositories import (
    InstallationEventValidationError,
    InstallationRepositoriesEvent,
    parse_installation_repositories_event,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestProjectionStatus,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent

_DEFERRED_PR_ACTIONS = frozenset(
    {
        "opened",
        "synchronize",
        "review_requested",
        "review_request_removed",
        "closed",
        "reopened",
    }
)
_DEFERRED_CI_EVENTS = frozenset({"check_suite", "workflow_run"})
_RUN_TRIGGER_PR_ACTIONS = frozenset({"opened", "synchronize", "review_requested", "reopened"})


@dataclass(frozen=True)
class VerifiedGitHubDelivery:
    """Transport-verified GitHub delivery ready for application dispatch.

    The entrypoint owns signature verification and JSON decoding. The delivery
    receipt is committed before this value reaches the installation dispatcher.
    """

    delivery_id: str
    event_name: str
    payload: Mapping[str, object]


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


class WebhookRunTriggerHandler(Protocol):
    async def on_pr(self, event: PullRequestEvent) -> None: ...

    async def on_ci(self, event: CiTriggerEvent) -> None: ...


class GitHubInstallationDeliveryDispatcher:
    """Safely route supported GitHub installation events to onboarding.

    Parsing happens before the installation lookup.  If the installation has
    not previously been linked to a Workspace, the delivery is acknowledged as
    ignored and neither GitHub tree I/O nor database writes for onboarding run.
    """

    def __init__(
        self,
        *,
        resolver: GitHubInstallationResolver,
        onboarding: InstallationOnboardingHandler,
        pull_request_projector: PullRequestProjectionHandler | None = None,
        pull_request_parser: Callable[[Mapping[str, object]], PullRequestEvent] | None = None,
        run_trigger: WebhookRunTriggerHandler | None = None,
        ci_parser: Callable[[str, Mapping[str, object]], CiTriggerEvent] | None = None,
    ) -> None:
        self._resolver = resolver
        self._onboarding = onboarding
        self._pull_request_projector = pull_request_projector
        self._pull_request_parser = pull_request_parser
        self._run_trigger = run_trigger
        self._ci_parser = ci_parser

    async def execute(self, delivery: VerifiedGitHubDelivery) -> InstallationDeliveryDispatchResult:
        action = delivery.payload.get("action")
        if delivery.event_name == "pull_request":
            if not isinstance(action, str) or action not in _DEFERRED_PR_ACTIONS:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
                )
            if self._pull_request_projector is None or self._pull_request_parser is None:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                )
            try:
                pr_event = self._pull_request_parser(delivery.payload)
            except ValueError:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
                )
            projection = await self._pull_request_projector.execute(pr_event)
            if projection == PullRequestProjectionStatus.UNKNOWN_REPOSITORY:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY
                )
            if projection not in {
                PullRequestProjectionStatus.PROJECTED,
                PullRequestProjectionStatus.IGNORED_STALE,
            }:
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
                )
            if action in _RUN_TRIGGER_PR_ACTIONS:
                if self._run_trigger is None:
                    return InstallationDeliveryDispatchResult(
                        status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                    )
                await self._run_trigger.on_pr(pr_event)
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.PROJECTED_PR
            )
        if delivery.event_name == "status" or (
            delivery.event_name in _DEFERRED_CI_EVENTS and action == "completed"
        ):
            if self._run_trigger is not None and self._ci_parser is not None:
                try:
                    ci_event = self._ci_parser(delivery.event_name, delivery.payload)
                except ValueError:
                    return InstallationDeliveryDispatchResult(
                        status=InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
                    )
                await self._run_trigger.on_ci(ci_event)
                return InstallationDeliveryDispatchResult(
                    status=InstallationDeliveryDispatchStatus.PROCESSED_CI
                )
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
            )
        if delivery.event_name not in {"installation", "installation_repositories"}:
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
            )

        try:
            event = parse_installation_repositories_event(
                event_name=delivery.event_name,
                payload=delivery.payload,
            )
        except InstallationEventValidationError:
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
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
