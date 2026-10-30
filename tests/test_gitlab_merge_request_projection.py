"""Typed GitLab projector and opaque receipt boundaries, with fake application ports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import pytest

from app.modules.integrations.gitlab.api.dispatch import GitLabWebhookDispatchAdapter, action_of
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabCredentialUnavailable,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.application.webhook_receipts import prepare_gitlab_receipt
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_github_pull_request import RunCancellationNotice
from app.modules.reviews.application.project_gitlab_merge_request import (
    CanonicalGitLabMergeRequest,
    CanonicalMergeRequestAccessDenied,
    CanonicalMergeRequestIdentityMismatch,
    CanonicalMergeRequestInvalidResponse,
    CanonicalMergeRequestNotReady,
    CanonicalMergeRequestRateLimited,
    CanonicalMergeRequestStale,
    CanonicalMergeRequestUnavailable,
    GitLabDiffRefs,
    GitLabMergeRequestCheckpoint,
    GitLabMergeRequestCommand,
    GitLabMergeRequestHandlingResult,
    GitLabMergeRequestIdentityConflict,
    GitLabMergeRequestProjectionResult,
    GitLabMergeRequestRecord,
    GitLabMergeRequestState,
    HandleGitLabMergeRequest,
    LockedGitLabMergeRequest,
    ProjectGitLabMergeRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    UNSET,
    EnqueueResult,
    EnqueueStatus,
    UnsetType,
)

TARGET = BoundGitLabTarget(UUID(int=1), UUID(int=2), UUID(int=3), "https://gitlab.example", 17, 1)
COMMAND = GitLabMergeRequestCommand(UUID(int=1), 17, 901, 12, "open")
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
CURRENT = CanonicalGitLabMergeRequest(
    TARGET,
    901,
    17,
    12,
    17,
    29,
    GitLabMergeRequestState.OPENED,
    "Canonical title",
    "Canonical body",
    "author",
    "https://gitlab.example/team/repo/-/merge_requests/12",
    "feature",
    "main",
    NOW,
    GitLabDiffRefs("a" * 40, "b" * 40, "c" * 40),
)


class Authority:
    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
        assert identity == GitLabTargetIdentity(UUID(int=1), 17)
        return TARGET

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        assert target == TARGET


class Lock:
    active = False

    @asynccontextmanager
    async def hold(self, command: GitLabMergeRequestCommand) -> AsyncIterator[None]:
        assert command.origin_provider_installation_id == COMMAND.origin_provider_installation_id
        self.active = True
        try:
            yield
        finally:
            self.active = False


class Provider:
    def __init__(self, lock: Lock) -> None:
        self.lock = lock
        self.current = CURRENT

    async def get_current(
        self,
        command: GitLabMergeRequestCommand,
        *,
        target: BoundGitLabTarget,
        checkpoint: GitLabMergeRequestCheckpoint,
    ) -> CanonicalGitLabMergeRequest:
        assert self.lock.active
        await checkpoint()
        return self.current


class Store:
    def __init__(self) -> None:
        self.record: GitLabMergeRequestRecord | None = None
        self.saved: CanonicalGitLabMergeRequest | None = None

    async def get_or_create_locked(
        self, current: CanonicalGitLabMergeRequest, now: datetime
    ) -> LockedGitLabMergeRequest:
        created = self.record is None
        if self.record is None:
            self.record = GitLabMergeRequestRecord(
                UUID(int=4), current.diff_refs.head_sha, current.provider_updated_at, now
            )
        return LockedGitLabMergeRequest(self.record, created)

    async def save(
        self, record: GitLabMergeRequestRecord, current: CanonicalGitLabMergeRequest
    ) -> None:
        self.record = record
        self.saved = current


class Uow:
    def __init__(self) -> None:
        self.merge_requests = Store()
        self.runs = Runs()
        self.commits = 0
        self.locked_target: BoundGitLabTarget | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def lock_target(self, target: BoundGitLabTarget) -> None:
        self.locked_target = target

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class Runs:
    def __init__(self) -> None:
        self.cancellations: list[tuple[BoundGitLabTarget, UUID, str, str]] = []

    async def cancel_for_merge_request(
        self,
        target: BoundGitLabTarget,
        code_change_id: UUID,
        reason: str,
        now: datetime,
        *,
        current_head: str,
    ) -> tuple[RunCancellationNotice, ...]:
        self.cancellations.append((target, code_change_id, reason, current_head))
        return ()

    async def notify_run_updated(self, notice: RunCancellationNotice) -> None:
        raise AssertionError("No run notification for initial projection")


def test_projects_canonical_snapshot_and_keeps_literal_start_ref() -> None:
    async def scenario() -> None:
        uow = Uow()
        lock = Lock()
        projector = ProjectGitLabMergeRequest(
            authority=Authority(),
            current_provider=Provider(lock),
            projection_lock=lock,
            uow_factory=lambda: uow,
            now=lambda: NOW,
        )
        result = await projector.execute(COMMAND)
        assert result.target == TARGET
        assert result.code_change_id == UUID(int=4)
        assert result.current_head == "a" * 40
        assert result.created is True
        assert result.head_changed is True
        assert uow.commits == 1
        assert uow.locked_target == TARGET
        assert uow.merge_requests.record is not None
        assert uow.merge_requests.saved is not None
        assert uow.merge_requests.saved.diff_refs.start_sha == "b" * 40
        assert uow.merge_requests.saved.diff_refs.base_sha == "c" * 40
        assert not lock.active

    asyncio.run(scenario())


def projector(
    uow: Uow, current: CanonicalGitLabMergeRequest = CURRENT
) -> ProjectGitLabMergeRequest:
    lock = Lock()
    provider = Provider(lock)
    provider.current = current
    return ProjectGitLabMergeRequest(
        authority=Authority(),
        current_provider=provider,
        projection_lock=lock,
        uow_factory=lambda: uow,
        now=lambda: NOW + timedelta(hours=1),
    )


def test_same_head_title_update_preserves_clock_and_creates_no_work() -> None:
    async def scenario() -> None:
        uow = Uow()
        await projector(uow).execute(COMMAND)
        first_clock = (
            uow.merge_requests.record.head_first_seen_at if uow.merge_requests.record else None
        )
        result = await projector(uow, replace(CURRENT, title="New canonical title")).execute(
            COMMAND
        )
        assert result.created is False
        assert result.head_changed is False
        assert uow.merge_requests.record is not None
        assert uow.merge_requests.record.head_first_seen_at == first_clock
        assert uow.merge_requests.saved is not None
        assert uow.merge_requests.saved.title == "New canonical title"
        assert uow.runs.cancellations == []

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["open", "reopen", "update", "close", "merge"])
@pytest.mark.parametrize("state", [GitLabMergeRequestState.CLOSED, GitLabMergeRequestState.MERGED])
def test_current_terminal_state_wins_hook_action_and_cancels(
    action: str, state: GitLabMergeRequestState
) -> None:
    async def scenario() -> None:
        uow = Uow()
        result = await projector(uow, replace(CURRENT, state=state)).execute(
            replace(COMMAND, action=action)
        )
        assert result.state == state
        assert uow.runs.cancellations == [(TARGET, UUID(int=4), "pr_closed", "a" * 40)]

    asyncio.run(scenario())


def test_changed_head_cancels_only_obsolete_head_with_pinned_target() -> None:
    async def scenario() -> None:
        uow = Uow()
        await projector(uow).execute(COMMAND)
        result = await projector(
            uow, replace(CURRENT, diff_refs=GitLabDiffRefs("d" * 40, "e" * 40, "f" * 40))
        ).execute(COMMAND)
        assert result.head_changed is True
        assert uow.runs.cancellations == [(TARGET, UUID(int=4), "superseded", "d" * 40)]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("external_id", 902),
        ("iid", 13),
        ("project_id", 18),
        ("target_project_id", 18),
        ("target", replace(TARGET, credential_generation=2)),
    ],
)
def test_provider_cannot_apply_a_different_identity(field: str, value: Any) -> None:
    async def scenario() -> None:
        uow = Uow()
        wrong = replace(CURRENT, **{field: value})
        with pytest.raises(CanonicalMergeRequestIdentityMismatch):
            await projector(uow, wrong).execute(COMMAND)
        assert uow.commits == 0
        assert uow.merge_requests.record is None

    asyncio.run(scenario())


def test_strictly_older_canonical_stamp_defers_without_replacing_head() -> None:
    async def scenario() -> None:
        uow = Uow()
        await projector(uow).execute(COMMAND)
        older = replace(
            CURRENT,
            provider_updated_at=NOW - timedelta(seconds=1),
            diff_refs=GitLabDiffRefs("d" * 40, "e" * 40, "f" * 40),
        )
        with pytest.raises(ValueError, match="gitlab_merge_request_stale"):
            await projector(uow, older).execute(COMMAND)
        assert uow.commits == 1
        assert uow.merge_requests.record is not None
        assert uow.merge_requests.record.head_sha == "a" * 40

    asyncio.run(scenario())


def test_final_locked_authority_refusal_prevents_projection() -> None:
    class Refused(Uow):
        async def lock_target(self, target: BoundGitLabTarget) -> None:
            raise GitLabTargetInactive()

    async def scenario() -> None:
        uow = Refused()
        with pytest.raises(GitLabTargetInactive):
            await projector(uow).execute(COMMAND)
        assert uow.commits == 0
        assert uow.merge_requests.record is None

    asyncio.run(scenario())


class Handler:
    def __init__(self, error: Exception | None = None) -> None:
        self.commands: list[GitLabMergeRequestCommand] = []
        self.error = error

    async def execute(
        self, command: GitLabMergeRequestCommand
    ) -> GitLabMergeRequestProjectionResult:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return GitLabMergeRequestProjectionResult(
            TARGET, UUID(int=4), "a" * 40, GitLabMergeRequestState.OPENED, True, True
        )


def raw_receipt(
    data: dict[str, object] | None = None, *, event: str = "Merge Request Hook"
) -> WebhookReceipt:
    return prepare_gitlab_receipt(
        connection_id=TARGET.origin_provider_installation_id,
        webhook_id="stable-webhook-id",
        event_name=event,
        payload_json=json.dumps(
            data
            if data is not None
            else {
                "object_kind": "merge_request",
                "project": {"id": 17},
                "object_attributes": {"id": 901, "iid": 12, "action": "open"},
            }
        ),
    )


@pytest.mark.parametrize("action", ["open", "reopen", "update", "close", "merge"])
def test_dispatch_only_passes_typed_identity_and_never_payload_metadata(action: str) -> None:
    handler = Handler()
    delivery = raw_receipt(
        {
            "object_kind": "merge_request",
            "project": {"id": 17},
            "object_attributes": {
                "id": 901,
                "iid": 12,
                "action": action,
                "target_project_id": 17,
                "head_sha": "wrong",
                "title": "wrong",
                "state": "wrong",
                "url": "https://evil.example",
            },
            "workspace_id": "wrong",
            "installation": {"id": 999},
        }
    )
    result = asyncio.run(GitLabWebhookDispatchAdapter(handler).execute(delivery))
    assert result.status == InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert handler.commands == [replace(COMMAND, action=action)]
    assert action_of(delivery) == action


@pytest.mark.parametrize(
    "event,action", [("Merge Request Hook", "approved"), ("Push Hook", "open")]
)
def test_valid_unsupported_is_ignored_without_invoking_projector(event: str, action: str) -> None:
    handler = Handler()
    delivery = raw_receipt(
        {
            "object_kind": "merge_request",
            "project": {"id": 17},
            "object_attributes": {"id": 901, "iid": 12, "action": action},
        },
        event=event,
    )
    result = asyncio.run(GitLabWebhookDispatchAdapter(handler).execute(delivery))
    assert result.status == InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert handler.commands == []


@pytest.mark.parametrize(
    "attributes",
    [
        {"id": True, "iid": 12, "action": "open"},
        {"id": 901, "iid": True, "action": "open"},
        {"id": 0, "iid": 12, "action": "open"},
        {"id": 901, "iid": 2147483648, "action": "open"},
        {"id": 9223372036854775808, "iid": 12, "action": "open"},
        {"id": 901, "iid": 12, "action": "open", "target_project_id": 18},
        {"id": 901, "iid": 12, "action": "open", "target_project_id": None},
        {"id": 901, "iid": 12, "action": ""},
        {"iid": 12, "action": "open"},
    ],
)
def test_malformed_supported_shape_is_safe_invalid(attributes: dict[str, object]) -> None:
    delivery = replace(
        raw_receipt(),
        payload_json=json.dumps(
            {"object_kind": "merge_request", "project": {"id": 17}, "object_attributes": attributes}
        ),
    )
    handler = Handler()
    result = asyncio.run(GitLabWebhookDispatchAdapter(handler).execute(delivery))
    assert result.status == InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert result.detail == "invalid_payload"
    assert handler.commands == []


@pytest.mark.parametrize(
    "delivery",
    [
        replace(raw_receipt(), provider="github", origin_provider_installation_id=None),
        replace(raw_receipt(), delivery_id="bad-normalized-id"),
        replace(raw_receipt(), payload_json="not-json"),
    ],
)
def test_dispatch_requires_normalized_gitlab_immutable_receipt(delivery: WebhookReceipt) -> None:
    handler = Handler()
    result = asyncio.run(GitLabWebhookDispatchAdapter(handler).execute(delivery))
    assert result.status == InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT
    assert handler.commands == []


@pytest.mark.parametrize(
    "error,status,detail",
    [
        (
            GitLabTargetInactive(),
            InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT,
            "inactive_origin",
        ),
        (
            CanonicalMergeRequestNotReady(),
            InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
            "canonical_not_ready",
        ),
        (
            CanonicalMergeRequestStale(),
            InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
            "canonical_stale",
        ),
        (
            CanonicalMergeRequestIdentityMismatch(),
            InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT,
            "identity_mismatch",
        ),
        (
            GitLabMergeRequestIdentityConflict(),
            InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT,
            "identity_conflict",
        ),
    ],
)
def test_dispatch_expected_refusals_use_existing_finite_or_terminal_status(
    error: Exception,
    status: InstallationDeliveryDispatchStatus,
    detail: str,
) -> None:
    result = asyncio.run(GitLabWebhookDispatchAdapter(Handler(error)).execute(raw_receipt()))
    assert result.status == status
    assert result.detail == detail


@pytest.mark.parametrize(
    "error",
    [
        GitLabCredentialGenerationChanged(),
        GitLabCredentialUnavailable(),
        GitLabRequestUnavailable(),
        CanonicalMergeRequestUnavailable(),
        CanonicalMergeRequestInvalidResponse(),
        CanonicalMergeRequestAccessDenied(),
        CanonicalMergeRequestRateLimited(),
        AssertionError("programmer"),
    ],
)
def test_dispatch_operational_and_generation_failures_reach_existing_failure_policy(
    error: Exception,
) -> None:
    with pytest.raises(type(error)) as caught:
        asyncio.run(GitLabWebhookDispatchAdapter(Handler(error)).execute(raw_receipt()))
    assert caught.value is error


@pytest.mark.parametrize(
    "timeout", [True, False, 0, -1, float("nan"), float("inf"), 61, "1s", None]
)
def test_projection_lock_configuration_is_finite_and_nonboolean(timeout: Any) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
        SqlAlchemyGitLabMergeRequestProjectionLock,
        SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
    )

    with pytest.raises(ValueError, match="Invalid GitLab projection lock timeout"):
        SqlAlchemyGitLabMergeRequestProjectionLock(
            "postgresql+psycopg://unused", lock_timeout_seconds=timeout
        )
    with pytest.raises(ValueError, match="Invalid GitLab projection lock timeout"):
        SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(
            async_sessionmaker(), lock_timeout_seconds=timeout
        )


class Enqueuer:
    def __init__(self, lock: Lock, uow: Uow) -> None:
        self.lock, self.uow = lock, uow
        self.identities: list[WebhookRunIdentity | None] = []

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_identity: WebhookRunIdentity | None = None,
        triggered_by: str | None | UnsetType = UNSET,
    ) -> EnqueueResult:
        assert not self.lock.active
        assert self.uow.commits > 0
        assert self.uow.merge_requests.saved is not None
        assert code_change_id == UUID(int=4)
        assert expected_head_sha == "a" * 40
        self.identities.append(expected_identity)
        return EnqueueResult(EnqueueStatus.ENQUEUED, UUID(int=5))


def test_application_handler_triggers_same_head_with_full_pinned_identity_after_projection() -> (
    None
):
    async def scenario() -> None:
        lock, uow = Lock(), Uow()
        provider = Provider(lock)
        enqueue = Enqueuer(lock, uow)
        handler = HandleGitLabMergeRequest(
            projector=ProjectGitLabMergeRequest(
                authority=Authority(),
                current_provider=provider,
                projection_lock=lock,
                uow_factory=lambda: uow,
                now=lambda: NOW,
            ),
            enqueuer=enqueue,
        )
        first = await handler.execute(COMMAND)
        second = await handler.execute(replace(COMMAND, action="update"))
        assert len(enqueue.identities) == 2
        assert (
            enqueue.identities
            == [
                WebhookRunIdentity(
                    "gitlab",
                    UUID(int=1),
                    UUID(int=2),
                    UUID(int=3),
                    17,
                    "https://gitlab.example",
                    1,
                )
            ]
            * 2
        )
        assert first.enqueue == EnqueueResult(EnqueueStatus.ENQUEUED, UUID(int=5))
        assert second.projection.created is False
        assert second.projection.head_changed is False
        assert second.projection.target == TARGET

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["open", "reopen", "update", "close", "merge"])
@pytest.mark.parametrize("state", list(GitLabMergeRequestState))
def test_application_automatic_trigger_requires_supported_action_and_canonical_open(
    action: str,
    state: GitLabMergeRequestState,
) -> None:
    async def scenario() -> None:
        lock, uow = Lock(), Uow()
        provider = Provider(lock)
        provider.current = replace(CURRENT, state=state)
        enqueuer = Enqueuer(lock, uow)
        handler = HandleGitLabMergeRequest(
            projector=ProjectGitLabMergeRequest(
                authority=Authority(),
                current_provider=provider,
                projection_lock=lock,
                uow_factory=lambda: uow,
                now=lambda: NOW,
            ),
            enqueuer=enqueuer,
        )
        result = await handler.execute(replace(COMMAND, action=action))
        allowed = action in {"open", "reopen", "update"} and state == GitLabMergeRequestState.OPENED
        assert (result.enqueue is not None) is allowed
        assert len(enqueuer.identities) == int(allowed)
        assert result.projection.state == state

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "receipt_status"),
    [
        (EnqueueStatus.ENQUEUED, InstallationDeliveryDispatchStatus.PROJECTED_PR),
        (EnqueueStatus.PUBLICATION_PENDING, InstallationDeliveryDispatchStatus.PROJECTED_PR),
        (EnqueueStatus.INELIGIBLE, InstallationDeliveryDispatchStatus.PROJECTED_PR),
        (EnqueueStatus.UNCONFIGURED, InstallationDeliveryDispatchStatus.PROJECTED_PR),
        (EnqueueStatus.STALE, InstallationDeliveryDispatchStatus.PROJECTED_PR),
        (EnqueueStatus.DUPLICATE, InstallationDeliveryDispatchStatus.PROJECTED_PR),
        (EnqueueStatus.DEFERRED, InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER),
    ],
)
def test_dispatch_maps_typed_enqueue_status_without_rendering_untrusted_detail(
    status: EnqueueStatus,
    receipt_status: InstallationDeliveryDispatchStatus,
) -> None:
    class RichHandler:
        async def execute(
            self, command: GitLabMergeRequestCommand
        ) -> GitLabMergeRequestHandlingResult:
            projection = await Handler().execute(command)
            return GitLabMergeRequestHandlingResult(
                projection,
                EnqueueResult(
                    status,
                    UUID(int=5),
                    "arbitrary-secret-reason",
                    "arbitrary-secret-detail",
                ),
            )

    result = asyncio.run(GitLabWebhookDispatchAdapter(RichHandler()).execute(raw_receipt()))
    assert result.status == receipt_status
    assert result.detail == status.value


def test_dispatch_terminal_handling_result_and_legacy_projector_are_compatible() -> None:
    class TerminalHandler:
        async def execute(
            self, command: GitLabMergeRequestCommand
        ) -> GitLabMergeRequestHandlingResult:
            return GitLabMergeRequestHandlingResult(await Handler().execute(command), None)

    for handler in (TerminalHandler(), Handler()):
        result = asyncio.run(GitLabWebhookDispatchAdapter(handler).execute(raw_receipt()))
        assert result.status == InstallationDeliveryDispatchStatus.PROJECTED_PR
        assert result.detail is None


def test_dispatch_decodes_json_once_per_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = raw_receipt()
    calls = 0
    loads = json.loads

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counted)
    handler = Handler()
    result = asyncio.run(GitLabWebhookDispatchAdapter(handler).execute(delivery))
    assert result.status == InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert handler.commands == [COMMAND]
    assert calls == 1
