"""Public contracts for dispatching verified GitHub installation deliveries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

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
from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    RepositoryDetailsUnavailableError,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubRepositoryDetailsResponseError,
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
from app.modules.reviews.application.trigger_from_delivery import (
    CiTriggerEvent,
    ProjectedPullRequestTarget,
    TriggerFromDelivery,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult, EnqueueStatus
from tests.github_webhook_fixtures import load_github_webhook_fixture
from tests.trigger_uow import targets_uow

_DISPATCH_LOGGER = "app.modules.integrations.webhooks.api.dispatch"


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


def _pull_request_delivery(
    action: str, *, label: object = None, delivery_id: str = "delivery-label"
) -> VerifiedGitHubDelivery:
    payload: dict[str, object] = {
        "action": action,
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
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
    if label is not None:
        payload["label"] = label
    return VerifiedGitHubDelivery(delivery_id, "pull_request", payload)


@pytest.mark.parametrize(
    ("action", "expected_enqueue"),
    [("synchronize", True), ("opened", False), ("edited", False)],
)
def test_projected_pr_actions_recheck_current_head_only_on_synchronize(
    action: str, expected_enqueue: bool
) -> None:
    current_head = "c" * 40
    projected: list[str] = []
    enqueued: list[tuple[UUID, str]] = []

    class Projector:
        async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
            projected.append(event.action)
            return PullRequestProjectionStatus.PROJECTED

    class Targets:
        async def for_pr(self, event: PullRequestEvent) -> ProjectedPullRequestTarget:
            assert projected == [action]
            return ProjectedPullRequestTarget(
                UUID("11111111-1111-1111-1111-111111111111"), current_head
            )

        async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
            return ()

    class Enqueuer:
        async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult:
            enqueued.append((code_change_id, expected_head_sha))
            return EnqueueResult(EnqueueStatus.ENQUEUED)

    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(),
            onboarding=FakeOnboarding(),
            pull_request_projector=Projector(),
            run_trigger=TriggerFromDelivery(
                uow_factory=targets_uow(Targets()), enqueuer=Enqueuer()
            ),
        )
    )

    result = asyncio.run(adapter.execute(_pull_request_delivery(action).to_receipt()))

    assert result.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert projected == [action]
    assert enqueued == (
        [(UUID("11111111-1111-1111-1111-111111111111"), current_head)] if expected_enqueue else []
    )


@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
def test_exact_ai_review_label_reaches_typed_intent_projector_and_only_add_triggers(
    action: str,
) -> None:
    @dataclass
    class IntentProjector:
        events: list[PullRequestLabelEvent] = field(default_factory=list)

        async def execute(self, event: PullRequestLabelEvent) -> PullRequestProjectionStatus:
            self.events.append(event)
            return PullRequestProjectionStatus.PROJECTED

    @dataclass
    class Trigger:
        prs: list[PullRequestEvent] = field(default_factory=list)
        labels: list[PullRequestLabelEvent] = field(default_factory=list)

        async def on_pr(self, event: PullRequestEvent) -> None:
            self.prs.append(event)

        async def on_ci(self, event: CiTriggerEvent) -> None:
            pass

        async def on_label(self, event: PullRequestLabelEvent) -> None:
            self.labels.append(event)

    intent = IntentProjector()
    trigger = Trigger()
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(),
            onboarding=FakeOnboarding(),
            label_intent_projector=intent,
            run_trigger=trigger,
        )
    )

    result = asyncio.run(
        adapter.execute(_pull_request_delivery(action, label={"name": "ai-review"}).to_receipt())
    )

    assert result.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert len(intent.events) == 1
    assert intent.events[0].pull_request.action == action
    assert intent.events[0].label_name == "ai-review"
    assert trigger.prs == []
    assert trigger.labels == ([intent.events[0]] if action == "labeled" else [])


@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
def test_exact_ai_review_label_defers_until_intent_projector_is_configured(
    action: str,
) -> None:
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(), onboarding=FakeOnboarding()
        )
    )

    result = asyncio.run(
        adapter.execute(_pull_request_delivery(action, label={"name": "ai-review"}).to_receipt())
    )

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT


@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
def test_unrelated_label_is_ignored_before_intent_projection(action: str) -> None:
    class IntentProjector:
        async def execute(self, event: PullRequestLabelEvent) -> PullRequestProjectionStatus:
            raise AssertionError("unrelated label reached intent projector")

    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(),
            onboarding=FakeOnboarding(),
            label_intent_projector=IntentProjector(),
        )
    )

    result = asyncio.run(
        adapter.execute(_pull_request_delivery(action, label={"name": "AI-review"}).to_receipt())
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert result.detail == f"action={action} label is not ai-review"


@pytest.mark.parametrize("label", [None, {}, {"name": ""}, {"name": 17}, "ai-review"])
def test_malformed_label_is_rejected_at_transport_boundary(label: object) -> None:
    class TypedDispatcher:
        async def execute(self, event: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
            raise AssertionError("malformed label reached application")

    result = asyncio.run(
        GitHubWebhookDispatchAdapter(TypedDispatcher()).execute(
            _pull_request_delivery("labeled", label=label).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT


def _check_suite_delivery() -> VerifiedGitHubDelivery:
    return VerifiedGitHubDelivery(
        "delivery-ci",
        "check_suite",
        {
            "action": "completed",
            "installation": {"id": 17},
            "repository": {"id": 101, "full_name": "octo/repo"},
            "check_suite": {"head_sha": "a" * 40},
        },
    )


@pytest.mark.parametrize(
    ("delivery", "status", "detail"),
    [
        (
            _pull_request_delivery("labeled", label={"name": "ai-review"}),
            InstallationDeliveryDispatchStatus.PROJECTED_PR,
            "action=labeled label outcome",
        ),
        (
            _pull_request_delivery("synchronize"),
            InstallationDeliveryDispatchStatus.PROJECTED_PR,
            "action=synchronize pr outcome",
        ),
        (
            _check_suite_delivery(),
            InstallationDeliveryDispatchStatus.PROCESSED_CI,
            "ci outcome",
        ),
    ],
)
def test_the_run_trigger_outcome_becomes_the_dispatch_detail(
    delivery: VerifiedGitHubDelivery, status: InstallationDeliveryDispatchStatus, detail: str
) -> None:
    class Projector:
        async def execute(
            self, event: PullRequestEvent | PullRequestLabelEvent
        ) -> PullRequestProjectionStatus:
            return PullRequestProjectionStatus.PROJECTED

    class Trigger:
        async def on_pr(self, event: PullRequestEvent) -> str:
            return "pr outcome"

        async def on_label(self, event: PullRequestLabelEvent) -> str:
            return "label outcome"

        async def on_ci(self, event: CiTriggerEvent) -> str:
            return "ci outcome"

    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(),
            onboarding=FakeOnboarding(),
            pull_request_projector=Projector(),
            label_intent_projector=Projector(),
            run_trigger=Trigger(),
        )
    )

    result = asyncio.run(adapter.execute(delivery.to_receipt()))

    assert result == InstallationDeliveryDispatchResult(status, detail)


@pytest.mark.parametrize(
    ("projection", "status", "detail"),
    [
        (
            PullRequestProjectionStatus.UNKNOWN_REPOSITORY,
            InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY,
            "action=unlabeled unknown_repository",
        ),
        # A stored repository that refuses the event is deferred like an unknown one (#80).
        (
            PullRequestProjectionStatus.DISABLED_REPOSITORY,
            InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY,
            "action=unlabeled disabled_repository",
        ),
        (
            PullRequestProjectionStatus.OTHER_INSTALLATION_REPOSITORY,
            InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY,
            "action=unlabeled other_installation_repository",
        ),
        (
            PullRequestProjectionStatus.IGNORED_UNRELATED,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            "action=unlabeled ignored_unrelated",
        ),
        (
            PullRequestProjectionStatus.IGNORED_OWN_BOT,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            "action=unlabeled ignored_own_bot",
        ),
        (
            PullRequestProjectionStatus.IGNORED_IDENTITY_MISMATCH,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            "action=unlabeled ignored_identity_mismatch",
        ),
        (
            PullRequestProjectionStatus.IGNORED_IDENTITY_CONFLICT,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            "action=unlabeled ignored_identity_conflict",
        ),
        (
            PullRequestProjectionStatus.IGNORED_EXTERNAL_ID_MISMATCH,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            "action=unlabeled ignored_external_id_mismatch",
        ),
        (
            PullRequestProjectionStatus.IGNORED_OTHER_LABEL,
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            "action=unlabeled ignored_other_label",
        ),
        (
            PullRequestProjectionStatus.IGNORED_STALE,
            InstallationDeliveryDispatchStatus.PROJECTED_PR,
            "action=unlabeled ignored_stale",
        ),
    ],
)
def test_a_projection_that_did_not_apply_the_label_names_itself_in_the_detail(
    projection: PullRequestProjectionStatus,
    status: InstallationDeliveryDispatchStatus,
    detail: str,
) -> None:
    class IntentProjector:
        async def execute(self, event: PullRequestLabelEvent) -> PullRequestProjectionStatus:
            return projection

    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(),
            onboarding=FakeOnboarding(),
            label_intent_projector=IntentProjector(),
        )
    )

    result = asyncio.run(
        adapter.execute(
            _pull_request_delivery("unlabeled", label={"name": "ai-review"}).to_receipt()
        )
    )

    assert result == InstallationDeliveryDispatchResult(status, detail)


@pytest.mark.parametrize("action", ["review_requested", "review_request_removed"])
def test_former_reviewer_actions_do_not_project_intent_or_enqueue(action: str) -> None:
    class Projector:
        async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
            raise AssertionError("former reviewer action reached PR projector")

    class Trigger:
        async def on_pr(self, event: PullRequestEvent) -> None:
            raise AssertionError("former reviewer action enqueued")

        async def on_label(self, event: PullRequestLabelEvent) -> None:
            raise AssertionError("former reviewer action enqueued")

        async def on_ci(self, event: CiTriggerEvent) -> None:
            pass

    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=FakeInstallationResolver(),
            onboarding=FakeOnboarding(),
            pull_request_projector=Projector(),
            run_trigger=Trigger(),
        )
    )

    result = asyncio.run(adapter.execute(_pull_request_delivery(action).to_receipt()))

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert result.detail == f"action={action}"


def test_dispatches_a_verified_delivery_to_the_existing_installation() -> None:
    installation_id = uuid4()
    resolver = FakeInstallationResolver(installations={17: installation_id})
    onboarding = FakeOnboarding()

    result = asyncio.run(
        GitHubWebhookDispatchAdapter(
            GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
        ).execute(_added_delivery().to_receipt())
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
        GitHubWebhookDispatchAdapter(
            GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
        ).execute(_added_delivery().to_receipt())
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION
    assert resolver.calls == [17]
    assert onboarding.calls == []


_DISPATCHER_LOGGER = "app.modules.integrations.webhooks.application.github_installation_dispatch"


@dataclass
class RaisingOnboarding:
    error: Exception

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        event: InstallationRepositoriesEvent,
    ) -> tuple[OnboardingResult, ...]:
        raise self.error


def test_unreadable_repository_details_defer_the_delivery_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GitHub cannot answer now: the receipt is deferred, not failed (api#71 AC2)."""
    onboarding = RaisingOnboarding(
        RepositoryDetailsUnavailableError("GitHub repository details request failed with HTTP 404")
    )
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=FakeInstallationResolver(installations={17: uuid4()}), onboarding=onboarding
    )

    with caplog.at_level(logging.WARNING, logger=_DISPATCHER_LOGGER):
        result = asyncio.run(
            GitHubWebhookDispatchAdapter(dispatcher).execute(_added_delivery().to_receipt())
        )

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    warnings = [
        record
        for record in caplog.records
        if record.name == _DISPATCHER_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    logged = warnings[0].getMessage()
    assert "17" in logged
    assert "HTTP 404" in logged
    for payload_value in ("octo/api", "https://github.com/octo/api", "delivery-1"):
        assert payload_value not in caplog.text


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), GitHubRepositoryDetailsResponseError("no default_branch")],
    ids=["unexpected-failure", "permanent-response-fault"],
)
def test_any_other_onboarding_failure_still_propagates_instead_of_deferring(
    error: Exception,
) -> None:
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=FakeInstallationResolver(installations={17: uuid4()}),
        onboarding=RaisingOnboarding(error),
    )

    with pytest.raises(type(error)) as raised:
        asyncio.run(
            GitHubWebhookDispatchAdapter(dispatcher).execute(_added_delivery().to_receipt())
        )

    assert raised.value is error


def test_a_transient_installation_token_failure_defers_the_delivery_like_unreadable_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A token GitHub could not issue is a read it cannot answer: deferred, not failed."""
    token_error = InstallationAccessTokenError(
        "GitHub installation access token unavailable", transient=True
    )
    token_error.__cause__ = RuntimeError("SENTINEL-token-response")
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=FakeInstallationResolver(installations={17: uuid4()}),
        onboarding=RaisingOnboarding(token_error),
    )

    with caplog.at_level(logging.WARNING, logger=_DISPATCHER_LOGGER):
        result = asyncio.run(
            GitHubWebhookDispatchAdapter(dispatcher).execute(_added_delivery().to_receipt())
        )

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    warnings = [
        record
        for record in caplog.records
        if record.name == _DISPATCHER_LOGGER and record.levelno == logging.WARNING
    ]
    assert [record.getMessage() for record in warnings] == [
        "Repository details unavailable for installation 17: "
        "GitHub installation access token unavailable"
    ]
    assert warnings[0].exc_info is None
    for value in ("SENTINEL", "octo/api", "https://github.com/octo/api", "delivery-1"):
        assert value not in caplog.text


def test_a_permanent_installation_token_failure_still_propagates_instead_of_deferring(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed token response or an unusable App key is permanent (api#71): failed path."""
    token_error = InstallationAccessTokenError(
        "GitHub installation access token unavailable", transient=False
    )
    token_error.__cause__ = ValueError("SENTINEL-token-response")
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=FakeInstallationResolver(installations={17: uuid4()}),
        onboarding=RaisingOnboarding(token_error),
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(InstallationAccessTokenError) as raised,
    ):
        asyncio.run(
            GitHubWebhookDispatchAdapter(dispatcher).execute(_added_delivery().to_receipt())
        )

    assert raised.value is token_error
    assert [record for record in caplog.records if record.name == _DISPATCHER_LOGGER] == []
    for value in ("SENTINEL", "octo/api", "https://github.com/octo/api", "delivery-1"):
        assert value not in caplog.text


def test_malformed_or_unsupported_delivery_is_ignored_before_lookup() -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    dispatcher = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
    )

    malformed = asyncio.run(
        dispatcher.execute(
            VerifiedGitHubDelivery(
                delivery_id="delivery-2",
                event_name="installation_repositories",
                payload={"action": "added", "installation": {"id": 17}},
            ).to_receipt()
        )
    )
    unsupported = asyncio.run(
        dispatcher.execute(
            VerifiedGitHubDelivery(
                delivery_id="delivery-3", event_name="push", payload={}
            ).to_receipt()
        )
    )

    assert malformed.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert unsupported.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert unsupported.detail is None  # a push has no action to report
    assert resolver.calls == []
    assert onboarding.calls == []


@pytest.mark.parametrize("event_name", ["pull_request", "check_suite", "workflow_run", "status"])
def test_future_actionable_delivery_is_deferred_without_installation_lookup(
    event_name: str,
) -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    dispatcher = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding),
    )
    head = "a" * 40
    payload: dict[str, object] = {
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
    }
    if event_name == "pull_request":
        payload.update(
            {
                "action": "opened",
                "pull_request": {
                    "id": 901,
                    "number": 7,
                    "title": "Review parser",
                    "html_url": "https://github.com/octo/repo/pull/7",
                    "user": {"login": "alice"},
                    "head": {"ref": "feature", "sha": head},
                    "base": {"ref": "main", "sha": "b" * 40},
                    "state": "open",
                    "updated_at": "2026-09-28T11:59:00Z",
                },
            }
        )
    elif event_name == "status":
        payload["sha"] = head
    else:
        payload["action"] = "completed"
        payload[event_name] = {"head_sha": head}

    result = asyncio.run(
        dispatcher.execute(VerifiedGitHubDelivery("future-1", event_name, payload).to_receipt())
    )

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
    assert resolver.calls == []
    assert onboarding.calls == []


def test_non_actionable_pr_action_is_ignored() -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    dispatcher = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
    )

    result = asyncio.run(
        dispatcher.execute(
            VerifiedGitHubDelivery(
                "irrelevant-1", "pull_request", {"action": "ready_for_review"}
            ).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert result.detail == "action=ready_for_review"
    assert resolver.calls == []
    assert onboarding.calls == []


@pytest.mark.parametrize(
    ("event_name", "action", "detail"),
    [
        ("pull_request", "ready_for_review", "action=ready_for_review"),
        ("pull_request", "ready_for_review\nFAKE LINE", None),
        ("pull_request", "Ready For Review", None),
        ("installation", "suspend", "action=suspend"),
        ("installation", "suspend\nFAKE LINE", None),
        ("installation_repositories", "x" * 41, None),
        ("push", "created", "action=created"),
        ("push", "created\nFAKE LINE", None),
    ],
)
def test_an_ignored_events_action_reaches_the_detail_only_as_a_plain_token(
    event_name: str, action: str, detail: str | None
) -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
    )

    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery(
                "ignored-1", event_name, {"action": action, "installation": {"id": 17}}
            ).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert result.detail == detail
    assert resolver.calls == []


def test_replay_adapter_delivers_typed_pr_ci_and_installation_events() -> None:
    @dataclass
    class TypedDispatcher:
        received: list[GitHubDispatchEvent] = field(default_factory=list)

        async def execute(self, event: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
            self.received.append(event)
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    dispatcher = TypedDispatcher()
    adapter = GitHubWebhookDispatchAdapter(dispatcher)
    head = "a" * 40
    base = "b" * 40
    pr = VerifiedGitHubDelivery(
        "delivery-pr",
        "pull_request",
        {
            "action": "opened",
            "installation": {"id": 17},
            "repository": {"id": 101, "full_name": "octo/repo"},
            "pull_request": {
                "id": 901,
                "number": 7,
                "title": "Review parser",
                "html_url": "https://github.com/octo/repo/pull/7",
                "user": {"login": "alice"},
                "head": {"ref": "feature", "sha": head},
                "base": {"ref": "main", "sha": base},
                "state": "open",
                "updated_at": "2026-09-28T11:59:00Z",
            },
        },
    )
    ci = VerifiedGitHubDelivery(
        "delivery-ci",
        "check_suite",
        {
            "action": "completed",
            "installation": {"id": 17},
            "repository": {"id": 101},
            "check_suite": {"head_sha": head},
        },
    )
    for delivery in (pr, ci, _added_delivery()):
        assert asyncio.run(adapter.execute(delivery.to_receipt())).status == "onboarded"

    assert [event.delivery_id for event in dispatcher.received] == [
        "delivery-pr",
        "delivery-ci",
        "delivery-1",
    ]
    assert isinstance(dispatcher.received[0].value, PullRequestEvent)
    assert dispatcher.received[0].value.title == "Review parser"
    assert isinstance(dispatcher.received[1].value, CiTriggerEvent)
    assert dispatcher.received[1].value.head_sha == head
    assert isinstance(dispatcher.received[2].value, InstallationRepositoriesEvent)
    assert dispatcher.received[2].value.installation_external_id == 17


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("pull_request", {"action": "opened", "installation": {"id": 17}}),
        ("check_suite", {"action": "completed", "installation": {"id": 17}}),
        ("installation", {"action": "created", "installation": {"id": 17}}),
    ],
)
def test_malformed_supported_event_fails_closed_at_replay_boundary(
    event_name: str, payload: dict[str, object]
) -> None:
    resolver = FakeInstallationResolver()
    onboarding = FakeOnboarding()
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
    )

    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery("delivery-malformed", event_name, payload).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert resolver.calls == []
    assert onboarding.calls == []


@pytest.mark.parametrize(
    ("event_name", "fixture", "installation_id"),
    [
        ("installation", "installation_created", 1000001),
        ("installation_repositories", "installation_repositories_added", 1000001),
    ],
)
def test_real_installation_delivery_reaches_onboarding_without_branch_and_url(
    event_name: str, fixture: str, installation_id: int
) -> None:
    """The five-field repository items GitHub really sends are accepted (api#71)."""
    local_id = uuid4()
    resolver = FakeInstallationResolver(installations={installation_id: local_id})
    onboarding = FakeOnboarding()

    result = asyncio.run(
        GitHubWebhookDispatchAdapter(
            GitHubInstallationDeliveryDispatcher(resolver=resolver, onboarding=onboarding)
        ).execute(
            VerifiedGitHubDelivery(
                "delivery-real", event_name, load_github_webhook_fixture(fixture)
            ).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.ONBOARDED
    assert resolver.calls == [installation_id]
    assert len(onboarding.calls) == 1
    assert onboarding.calls[0][0] == local_id
    repository = onboarding.calls[0][1].added_repositories[0]
    assert repository.default_branch is None
    assert repository.web_url is None


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
            VerifiedGitHubDelivery("delivery-push", "push", {"action": "created"}).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert dispatcher.events == [
        GitHubDispatchEvent("delivery-push", UnsupportedGitHubEvent("push", "created"))
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
