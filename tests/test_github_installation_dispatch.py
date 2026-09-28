"""Public contracts for dispatching verified GitHub installation deliveries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    InstallationDeliveryDispatchStatus,
    VerifiedGitHubDelivery,
)
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


def _added_delivery() -> VerifiedGitHubDelivery:
    return VerifiedGitHubDelivery(
        delivery_id="delivery-1",
        event_name="installation_repositories",
        payload={
            "action": "added",
            "installation": {"id": 17},
            "repositories_added": [
                {
                    "id": 101,
                    "full_name": "octo/api",
                    "default_branch": "main",
                    "html_url": "https://github.com/octo/api",
                }
            ],
        },
    )


def test_dispatches_a_verified_delivery_to_the_existing_installation() -> None:
    installation_id = uuid4()
    resolver = FakeInstallationResolver(installations={17: installation_id})
    onboarding = FakeOnboarding()

    result = asyncio.run(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding).execute(
            _added_delivery()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.ONBOARDED
    assert resolver.calls == [17]
    assert len(onboarding.calls) == 1
    assert onboarding.calls[0][0] == installation_id
    assert onboarding.calls[0][1].installation_external_id == 17


def test_unknown_installation_is_ignored_without_calling_onboarding() -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()

    result = asyncio.run(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding).execute(
            _added_delivery()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION
    assert resolver.calls == [17]
    assert onboarding.calls == []


def test_malformed_or_unsupported_delivery_is_ignored_before_lookup() -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    dispatcher = GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)

    malformed = asyncio.run(
        dispatcher.execute(
            VerifiedGitHubDelivery(
                delivery_id="delivery-2",
                event_name="installation_repositories",
                payload={"action": "added", "installation": {"id": 17}},
            )
        )
    )
    unsupported = asyncio.run(
        dispatcher.execute(
            VerifiedGitHubDelivery(delivery_id="delivery-3", event_name="push", payload={})
        )
    )

    assert malformed.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert unsupported.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert resolver.calls == []
    assert onboarding.calls == []


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("pull_request", {"action": "opened"}),
        ("check_suite", {"action": "completed"}),
        ("workflow_run", {"action": "completed"}),
        ("status", {"state": "success"}),
    ],
)
def test_future_actionable_delivery_is_deferred_without_installation_lookup(
    event_name: str, payload: dict[str, object]
) -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    dispatcher = GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)

    result = asyncio.run(
        dispatcher.execute(VerifiedGitHubDelivery("future-1", event_name, payload))
    )

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
    assert resolver.calls == []
    assert onboarding.calls == []


def test_non_actionable_pr_action_is_ignored() -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    dispatcher = GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)

    result = asyncio.run(
        dispatcher.execute(
            VerifiedGitHubDelivery("irrelevant-1", "pull_request", {"action": "labeled"})
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert resolver.calls == []
    assert onboarding.calls == []
