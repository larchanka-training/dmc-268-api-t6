"""Fakes of the dispatcher's installation ports, shared by the dispatch test files."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult


@dataclass
class FakeInstallationResolver:
    installations: dict[int, UUID] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)

    async def find_github_installation_id(self, external_id: int) -> UUID | None:
        self.calls.append(external_id)
        return self.installations.get(external_id)


@dataclass
class FakeOnboarding:
    calls: list[tuple[UUID, InstallationRepositoriesEvent]] = field(default_factory=list)

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        event: InstallationRepositoriesEvent,
    ) -> tuple[OnboardingResult, ...]:
        self.calls.append((provider_installation_id, event))
        return ()
