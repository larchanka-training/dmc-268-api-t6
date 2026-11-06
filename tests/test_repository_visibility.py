"""Webhook events that keep the saved visibility of a repository current (#454)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult

_DISPATCH_LOGGER = "app.modules.integrations.webhooks.application.github_installation_dispatch"
RECEIVED_AT = datetime(2026, 11, 1, 12, tzinfo=UTC)


@dataclass
class _Visibility:
    known: bool = True
    error: Exception | None = None
    overwritten: list[tuple[int, int, bool, datetime]] = field(default_factory=list)
    learned: list[tuple[int, int, bool, datetime]] = field(default_factory=list)
    forgotten: list[tuple[int, datetime]] = field(default_factory=list)

    async def forget(self, installation_external_id: int, observed_at: datetime) -> bool:
        self.forgotten.append((installation_external_id, observed_at))
        return self.known

    async def overwrite(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> bool:
        self.overwritten.append(
            (installation_external_id, repository_external_id, private, observed_at)
        )
        return self.known

    async def learn(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> None:
        self.learned.append(
            (installation_external_id, repository_external_id, private, observed_at)
        )
        if self.error is not None:
            raise self.error


class _Resolver:
    async def find_github_installation_id(self, external_id: int) -> UUID | None:
        raise AssertionError("a visibility event resolves no installation")


class _Onboarding:
    async def execute(
        self, *, provider_installation_id: UUID, event: InstallationRepositoriesEvent
    ) -> tuple[OnboardingResult, ...]:
        raise AssertionError("a visibility event onboards nothing")


def _dispatch(
    visibility: _Visibility | None, event_name: str, payload: dict[str, Any]
) -> InstallationDeliveryDispatchResult:
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=_Resolver(), onboarding=_Onboarding(), repository_visibility=visibility
        )
    )
    receipt = VerifiedGitHubDelivery("delivery-1", event_name, payload).to_receipt()
    return asyncio.run(adapter.execute(replace(receipt, received_at=RECEIVED_AT)))


def _repository_event(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
    }


def _pull_request_event(action: str, **repository: object) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo", **repository},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Review parser",
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
            "state": "open",
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }


@pytest.mark.parametrize(("action", "private"), [("privatized", True), ("publicized", False)])
def test_a_visibility_change_overwrites_the_saved_flag(action: str, private: bool) -> None:
    visibility = _Visibility()

    result = _dispatch(visibility, "repository", _repository_event(action))

    assert visibility.overwritten == [(17, 101, private, RECEIVED_AT)]
    assert visibility.learned == []
    assert result == InstallationDeliveryDispatchResult(
        InstallationDeliveryDispatchStatus.UPDATED_REPOSITORY_VISIBILITY
    )


def test_a_visibility_change_of_a_repository_not_connected_is_final() -> None:
    result = _dispatch(_Visibility(known=False), "repository", _repository_event("privatized"))

    assert result == InstallationDeliveryDispatchResult(
        InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT, "unknown_repository"
    )


@pytest.mark.parametrize("action", ["renamed", "edited", "transferred", "deleted", "archived"])
def test_other_repository_actions_change_nothing(action: str) -> None:
    visibility = _Visibility()

    result = _dispatch(visibility, "repository", _repository_event(action))

    assert visibility.overwritten == []
    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"installation": None}, id="no-installation"),
        pytest.param({"repository": {"id": "101"}}, id="id-not-a-number"),
    ],
)
def test_a_malformed_visibility_change_is_invalid(change: dict[str, Any]) -> None:
    visibility = _Visibility()

    result = _dispatch(visibility, "repository", _repository_event("privatized") | change)

    assert visibility.overwritten == []
    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT


def test_a_suspended_installation_forgets_the_saved_flags() -> None:
    visibility = _Visibility()

    result = _dispatch(
        visibility, "installation", {"action": "suspend", "installation": {"id": 17}}
    )

    assert visibility.forgotten == [(17, RECEIVED_AT)]
    assert result == InstallationDeliveryDispatchResult(
        InstallationDeliveryDispatchStatus.UPDATED_REPOSITORY_VISIBILITY
    )


def test_a_suspended_installation_with_no_repositories_is_final() -> None:
    result = _dispatch(
        _Visibility(known=False), "installation", {"action": "suspend", "installation": {"id": 17}}
    )

    assert result == InstallationDeliveryDispatchResult(
        InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT, "unknown_installation"
    )


def test_a_suspension_without_the_handler_is_deferred() -> None:
    result = _dispatch(None, "installation", {"action": "suspend", "installation": {"id": 17}})

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        pytest.param("installation", {"action": "unsuspend"}, id="unsuspend"),
        pytest.param("installation", {"action": "new_permissions_accepted"}, id="permissions"),
        pytest.param("installation_repositories", {"action": "suspend"}, id="another-event"),
    ],
)
def test_nothing_else_forgets_the_saved_flags(event_name: str, payload: dict[str, Any]) -> None:
    visibility = _Visibility()

    result = _dispatch(visibility, event_name, payload | {"installation": {"id": 17}})

    assert visibility.forgotten == []
    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT


def test_a_malformed_suspension_is_invalid() -> None:
    visibility = _Visibility()

    result = _dispatch(
        visibility, "installation", {"action": "suspend", "installation": {"id": "17"}}
    )

    assert visibility.forgotten == []
    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT


def test_a_visibility_change_without_the_handler_is_deferred() -> None:
    result = _dispatch(None, "repository", _repository_event("privatized"))

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize(
    "payload_change",
    [
        pytest.param({}, id="synchronize"),
        pytest.param({"action": "review_requested"}, id="ignored-action"),
        pytest.param(
            {"action": "labeled", "label": {"name": "bug"}},
            id="label-that-is-not-ai-review",
        ),
    ],
)
def test_a_pull_request_event_teaches_the_flag_of_its_payload(
    private: bool, payload_change: dict[str, Any]
) -> None:
    visibility = _Visibility()

    _dispatch(
        visibility,
        "pull_request",
        _pull_request_event("synchronize", private=private) | payload_change,
    )

    assert visibility.learned == [(17, 101, private, RECEIVED_AT)]
    assert visibility.overwritten == []


@pytest.mark.parametrize(
    "repository",
    [
        pytest.param({}, id="absent"),
        pytest.param({"private": None}, id="null"),
        pytest.param({"private": "false"}, id="not-a-boolean"),
    ],
)
def test_a_pull_request_event_without_a_usable_flag_teaches_nothing_and_still_counts(
    repository: dict[str, object],
) -> None:
    visibility = _Visibility()

    result = _dispatch(visibility, "pull_request", _pull_request_event("opened", **repository))

    assert visibility.learned == []
    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT


def test_a_failed_lesson_never_fails_the_delivery(caplog: pytest.LogCaptureFixture) -> None:
    visibility = _Visibility(error=RuntimeError("database is away"))

    with caplog.at_level(logging.ERROR, logger=_DISPATCH_LOGGER):
        result = _dispatch(visibility, "pull_request", _pull_request_event("opened", private=False))

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
    assert "Failed to record the visibility of repository 101 of installation 17" in caplog.text
