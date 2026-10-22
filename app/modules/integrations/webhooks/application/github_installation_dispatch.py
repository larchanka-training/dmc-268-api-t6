"""Dispatch verified GitHub installation deliveries to repository onboarding."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
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
    PullRequestState,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent, RunTriggerOutcome

_LOGGER = logging.getLogger(__name__)
_RUN_TRIGGER_PR_ACTIONS = frozenset({"reopened", "synchronize"})
_INDEX_REQUESTED = "index=requested"


def _with_action(action: str | None, detail: str | None) -> str | None:
    """Name the pull request action in the detail, so ``labeled`` differs from ``synchronize``."""
    if action is None:
        return detail
    return f"action={action}" if detail is None else f"action={action} {detail}"


def _with_index_requested(
    result: InstallationDeliveryDispatchResult,
) -> InstallationDeliveryDispatchResult:
    """The status stays; the detail records that the index update was requested (#143)."""
    detail = _INDEX_REQUESTED if result.detail is None else f"{result.detail} {_INDEX_REQUESTED}"
    return replace(result, detail=detail)


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
    DEFERRED_RUN_TRIGGER = "deferred_run_trigger"
    REQUESTED_RULES_REFRESH = "requested_rules_refresh"


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
class PushEvent:
    """A push; only one to the default branch can change the rules status (#150) or
    move the head the index follows (#143)."""

    installation_external_id: int
    repository_external_id: int
    ref: str
    default_branch: str
    # The head after the push; None when the push deleted the ref.
    after: str | None = None

    @property
    def to_default_branch(self) -> bool:
        return self.ref == f"refs/heads/{self.default_branch}"


@dataclass(frozen=True)
class GitHubDispatchEvent:
    """Validated application input from a claimed raw webhook receipt."""

    delivery_id: str
    value: (
        PullRequestEvent
        | PullRequestLabelEvent
        | CiTriggerEvent
        | InstallationRepositoriesEvent
        | PushEvent
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
    """Structured trigger detail and retry intent for each supported event."""

    async def on_pr(self, event: PullRequestEvent) -> RunTriggerOutcome: ...

    async def on_label(self, event: PullRequestLabelEvent) -> RunTriggerOutcome: ...

    async def on_ci(self, event: CiTriggerEvent) -> RunTriggerOutcome: ...


class RulesRefreshOnPushHandler(Protocol):
    """Request a new read of ``.review/rules.md``; False when the repository is not connected."""

    async def execute(self, installation_external_id: int, repository_external_id: int) -> bool: ...


class CodeIndexRefreshHandler(Protocol):
    """Request an index update up to ``commit_sha`` (only the wake-up marker when None).

    One short transaction, no GitHub call. False when the repository is not connected,
    its effective RAG is off, or it has no live index.
    """

    async def execute(
        self, installation_external_id: int, repository_external_id: int, commit_sha: str | None
    ) -> bool: ...


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
        rules_refresh: RulesRefreshOnPushHandler | None = None,
        index_refresh: CodeIndexRefreshHandler | None = None,
    ) -> None:
        self._resolver = resolver
        self._onboarding = onboarding
        self._pull_request_projector = pull_request_projector
        self._label_intent_projector = label_intent_projector
        self._run_trigger = run_trigger
        self._rules_refresh = rules_refresh
        self._index_refresh = index_refresh

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
                outcome = await self._run_trigger.on_label(event)
                return self._trigger_result(outcome, result.status, action)
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
            # Before the early return: a merge into a repository with paused reviews
            # still moves the default branch the index follows (ADR 0001 §6.1).
            if await self._refresh_index_on_merge(event):
                result = _with_index_requested(result)
            if result.status != InstallationDeliveryDispatchStatus.PROJECTED_PR:
                return result
            if event.action in _RUN_TRIGGER_PR_ACTIONS:
                if self._run_trigger is None:
                    return InstallationDeliveryDispatchResult(
                        status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
                        detail=_with_action(event.action, None),
                    )
                outcome = await self._run_trigger.on_pr(event)
                return self._trigger_result(outcome, result.status, event.action)
            return result
        if isinstance(event, PushEvent):
            return await self._push_result(event)
        if isinstance(event, CiTriggerEvent):
            if self._run_trigger is not None:
                outcome = await self._run_trigger.on_ci(event)
                return self._trigger_result(
                    outcome, InstallationDeliveryDispatchStatus.PROCESSED_CI
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
                event=replace(event, delivery_id=delivery.delivery_id),
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

    async def _refresh_index_on_merge(self, event: PullRequestEvent) -> bool:
        """A merge into the default branch named by the payload requests an index update
        up to its merge commit; the change set follows the indexed commit, not the PR. A
        failed request never fails the delivery of the committed projection."""
        if (
            self._index_refresh is None
            or event.action != "closed"
            or event.state is not PullRequestState.MERGED
            or event.repository_default_branch is None
            or event.target_branch != event.repository_default_branch
        ):
            return False
        return await self._request_index(
            self._index_refresh,
            event.installation_external_id,
            event.repository_external_id,
            event.merge_commit_sha,
        )

    async def _push_result(self, event: PushEvent) -> InstallationDeliveryDispatchResult:
        # The rules file and the indexed head change only by a commit to the default
        # branch; other refs and tags cannot change what the settings page shows.
        if not event.to_default_branch:
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
                detail="not_default_branch",
            )
        # Independent of the rules refresh, and idempotent like it; one status per
        # delivery, so the index outcome goes into the detail (ADR 0001 §6.1).
        index_requested = await self._refresh_index_on_push(event)
        result = await self._rules_refresh_result(event)
        # A repository the rules refresh does not know is reported as such alone.
        if index_requested and result.status != (
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
        ):
            return _with_index_requested(result)
        return result

    async def _refresh_index_on_push(self, event: PushEvent) -> bool:
        """A failed request never skips the rules refresh."""
        if self._index_refresh is None or event.after is None:
            return False
        return await self._request_index(
            self._index_refresh,
            event.installation_external_id,
            event.repository_external_id,
            event.after,
        )

    @staticmethod
    async def _request_index(
        refresh: CodeIndexRefreshHandler,
        installation_external_id: int,
        repository_external_id: int,
        commit_sha: str | None,
    ) -> bool:
        """A failed request is logged and reads as not requested, so it never fails the
        delivery; the leader's reconcile with the head catches the index up (ADR 0001
        §6.1)."""
        # CONTRACT: `Exception` only, so a cancellation still stops the delivery.
        try:
            return await refresh.execute(
                installation_external_id, repository_external_id, commit_sha
            )
        except Exception:
            _LOGGER.exception(
                "Failed to request an index update for repository %s of installation %s",
                repository_external_id,
                installation_external_id,
            )
            return False

    async def _rules_refresh_result(self, event: PushEvent) -> InstallationDeliveryDispatchResult:
        if self._rules_refresh is None:
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
            )
        if not await self._rules_refresh.execute(
            event.installation_external_id, event.repository_external_id
        ):
            # Not deferred: connecting the repository reads the file anyway.
            return InstallationDeliveryDispatchResult(
                status=InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
                detail="unknown_repository",
            )
        return InstallationDeliveryDispatchResult(
            status=InstallationDeliveryDispatchStatus.REQUESTED_RULES_REFRESH
        )

    @staticmethod
    def _trigger_result(
        outcome: RunTriggerOutcome,
        final_status: InstallationDeliveryDispatchStatus,
        action: str | None = None,
    ) -> InstallationDeliveryDispatchResult:
        status = (
            InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER
            if outcome.retry_required
            else final_status
        )
        return InstallationDeliveryDispatchResult(
            status, _with_action(action, outcome.detail or None)
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
