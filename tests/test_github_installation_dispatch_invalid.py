"""Invalid and unsupported GitHub installation deliveries at the dispatch boundary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    GitHubInstallationDeliveryDispatcher,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    UnsupportedGitHubEvent,
)
from tests.github_dispatch_fakes import FakeInstallationResolver, FakeOnboarding

_DISPATCH_LOGGER = "app.modules.integrations.webhooks.api.dispatch"


@pytest.mark.parametrize(
    ("event_name", "payload", "action", "installation_id", "location", "message"),
    [
        (
            "installation_repositories",
            {
                "action": "added",
                "installation": {"id": 17},
                "repositories_added": [
                    {
                        "id": 0,
                        "node_id": "SENTINEL-node-id",
                        "name": "SENTINEL-name",
                        "full_name": "SENTINEL-owner/SENTINEL-name",
                        "private": False,
                    }
                ],
            },
            "added",
            "17",
            "repositories_added.0.id",
            "greater than or equal to 1",
        ),
        (
            "installation",
            {
                "action": "created",
                "installation": {"id": 17},
                "repositories": [
                    {
                        "id": 1000004,
                        "node_id": "SENTINEL-node-id",
                        "name": "SENTINEL-name",
                        "full_name": "",
                        "private": False,
                    }
                ],
            },
            "created",
            "17",
            "repositories.0.full_name",
            "at least 1 character",
        ),
        (
            "installation_repositories",
            {
                "action": "removed",
                "installation": {"id": "SENTINEL-installation-id"},
                "repositories_removed": [],
            },
            "removed",
            "None",
            "installation.id",
            "valid integer",
        ),
    ],
)
def test_invalid_installation_event_is_ignored_with_a_warning_naming_the_failing_fields(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
    payload: dict[str, Any],
    action: str,
    installation_id: str,
    location: str,
    message: str,
) -> None:
    """The receipt is still ignored, but the operator can see why (api#71 AC3)."""
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
    )

    with caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER):
        result = asyncio.run(
            adapter.execute(
                VerifiedGitHubDelivery("delivery-invalid", event_name, payload).to_receipt()
            )
        )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert resolver.calls == []
    assert onboarding.calls == []
    warnings = [
        record
        for record in caplog.records
        if record.name == _DISPATCH_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    logged = warnings[0].getMessage()
    assert "delivery-invalid" in logged
    assert f"event={event_name}" in logged
    assert f"action={action}" in logged
    assert f"installation_id={installation_id}" in logged
    assert location in logged
    assert message in logged
    assert "SENTINEL" not in caplog.text


@pytest.mark.parametrize(
    ("event_name", "action", "array"),
    [
        ("installation_repositories", "added", "repositories_added"),
        ("installation_repositories", "removed", "repositories_removed"),
        ("installation", "created", "repositories"),
    ],
)
def test_repository_name_outside_owner_slash_repo_is_ignored_without_logging_the_value(
    caplog: pytest.LogCaptureFixture, event_name: str, action: str, array: str
) -> None:
    """A path-traversal-shaped ``full_name`` never reaches onboarding or a GitHub request."""
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
    )
    payload = {
        "action": action,
        "installation": {"id": 17},
        "repositories_added": [],
        "repositories_removed": [],
        array: [
            {
                "id": 101,
                "node_id": "SENTINEL-node-id",
                "name": "SENTINEL-name",
                "full_name": "SENTINEL-owner/../SENTINEL-name",
                "private": False,
            }
        ],
    }

    with caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER):
        result = asyncio.run(
            adapter.execute(
                VerifiedGitHubDelivery("delivery-bad-name", event_name, payload).to_receipt()
            )
        )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert resolver.calls == []
    assert onboarding.calls == []
    warnings = [
        record
        for record in caplog.records
        if record.name == _DISPATCH_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    logged = warnings[0].getMessage()
    assert f"event={event_name}" in logged
    assert f"action={action}" in logged
    assert f"{array}.0.full_name: String should match pattern" in logged
    assert "SENTINEL" not in caplog.text


def test_invalid_installation_event_warning_is_bounded_to_the_first_ten_field_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A signed event with many bad repositories still yields one short log line."""
    payload = {
        "action": "added",
        "installation": {"id": 17},
        "repositories_added": [
            {
                "node_id": f"SENTINEL-node-id-{index}",
                "name": "SENTINEL-name",
                "full_name": "SENTINEL-owner/SENTINEL-name",
                "private": False,
            }
            for index in range(25)
        ],
    }
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(), onboarding=FakeOnboarding()
        )
    )

    with caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER):
        result = asyncio.run(
            adapter.execute(
                VerifiedGitHubDelivery(
                    "delivery-many-invalid", "installation_repositories", payload
                ).to_receipt()
            )
        )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    warnings = [
        record
        for record in caplog.records
        if record.name == _DISPATCH_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    logged = warnings[0].getMessage()
    assert "error_count=25" in logged
    assert "(+15 more)" in logged
    for index in range(10):
        assert f"repositories_added.{index}.id: Field required" in logged
    assert "repositories_added.10.id" not in logged
    assert "SENTINEL" not in caplog.text


def test_unsupported_installation_action_is_not_logged_as_an_invalid_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(), onboarding=FakeOnboarding()
        )
    )

    with caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER):
        asyncio.run(
            adapter.execute(
                VerifiedGitHubDelivery(
                    "delivery-suspend",
                    "installation",
                    {"action": "suspend", "installation": {"id": 17}},
                ).to_receipt()
            )
        )

    assert [r for r in caplog.records if r.name == _DISPATCH_LOGGER] == []


def test_unsupported_receipt_reaches_application_as_event_metadata() -> None:
    @dataclass
    class TypedDispatcher:
        events: list[GitHubDispatchEvent] = field(default_factory=list)

        async def execute(self, event: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
            self.events.append(event)
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
            )

    dispatcher = TypedDispatcher()
    adapter = GitHubWebhookDispatchAdapter(dispatcher)
    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery(
                "delivery-deployment", "deployment", {"action": "created"}
            ).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert dispatcher.events == [
        GitHubDispatchEvent("delivery-deployment", UnsupportedGitHubEvent("deployment", "created"))
    ]


@pytest.mark.parametrize("event_name", ["installation", "installation_repositories"])
def test_unsupported_installation_action_reaches_application_as_event_metadata(
    event_name: str,
) -> None:
    @dataclass
    class TypedDispatcher:
        events: list[GitHubDispatchEvent] = field(default_factory=list)

        async def execute(self, event: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
            self.events.append(event)
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
            )

    dispatcher = TypedDispatcher()
    adapter = GitHubWebhookDispatchAdapter(dispatcher)
    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery(
                "delivery-installation",
                event_name,
                {"action": "suspend", "installation": {"id": 17}},
            ).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert dispatcher.events == [
        GitHubDispatchEvent("delivery-installation", UnsupportedGitHubEvent(event_name, "suspend"))
    ]


def test_invalid_receipt_never_enters_typed_dispatch() -> None:
    class TypedDispatcher:
        async def execute(self, event: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
            raise AssertionError("invalid receipt reached application dispatch")

    adapter = GitHubWebhookDispatchAdapter(TypedDispatcher())
    cases: tuple[tuple[str, dict[str, object], InstallationDeliveryDispatchStatus], ...] = (
        (
            "installation",
            {"action": "created", "installation": {"id": 17}},
            InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT,
        ),
        (
            "installation",
            {"action": "suspend", "installation": {"id": "17"}},
            InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT,
        ),
    )
    for event_name, payload, expected in cases:
        result = asyncio.run(
            adapter.execute(VerifiedGitHubDelivery("delivery", event_name, payload).to_receipt())
        )
        assert result.status is expected
