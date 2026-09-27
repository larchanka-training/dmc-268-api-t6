"""Dispatch verified GitHub installation deliveries to repository onboarding."""

from __future__ import annotations

from collections.abc import Mapping
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


@dataclass(frozen=True)
class VerifiedGitHubDelivery:
    """Transport-verified GitHub delivery ready for application dispatch.

    The future entrypoint owns signature verification and JSON decoding.  The
    delivery id is intentionally carried through this boundary although the
    current synchronous slice relies on idempotent repository onboarding rather
    than a durable delivery ledger.
    """

    delivery_id: str
    event_name: str
    payload: Mapping[str, object]


class InstallationDeliveryDispatchStatus(StrEnum):
    """Observable result for a verified installation delivery."""

    ONBOARDED = "onboarded"
    IGNORED_INVALID_EVENT = "ignored_invalid_event"
    IGNORED_UNKNOWN_INSTALLATION = "ignored_unknown_installation"


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
    ) -> None:
        self._resolver = resolver
        self._onboarding = onboarding

    async def execute(self, delivery: VerifiedGitHubDelivery) -> InstallationDeliveryDispatchResult:
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
