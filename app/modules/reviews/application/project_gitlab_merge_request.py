"""Pure canonical GitLab MR contracts; durable projection is composed separately."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabTargetIdentity,
    GitLabTargetReader,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_github_pull_request import RunCancellationNotice
from app.modules.reviews.application.try_enqueue_webhook_run import (
    UNSET,
    EnqueueResult,
    UnsetType,
)


class GitLabMergeRequestAction(StrEnum):
    OPEN = "open"
    REOPEN = "reopen"
    UPDATE = "update"
    CLOSE = "close"
    MERGE = "merge"


class GitLabMergeRequestState(StrEnum):
    OPENED = "opened"
    CLOSED = "closed"
    MERGED = "merged"


class CanonicalMergeRequestNotReady(ValueError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_not_ready")


class CanonicalMergeRequestIdentityMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_identity_mismatch")


class CanonicalMergeRequestInvalidResponse(ValueError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_invalid_response")


class CanonicalMergeRequestUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_unavailable")


class CanonicalMergeRequestAccessDenied(RuntimeError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_access_denied")


class CanonicalMergeRequestRateLimited(RuntimeError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_rate_limited")


@dataclass(frozen=True)
class GitLabMergeRequestCommand:
    origin_provider_installation_id: UUID
    project_id: int
    external_id: int
    iid: int
    action: str

    def __post_init__(self) -> None:
        GitLabTargetIdentity(self.origin_provider_installation_id, self.project_id)
        if (
            type(self.external_id) is not int
            or not 0 < self.external_id <= 9223372036854775807
            or type(self.iid) is not int
            or not 0 < self.iid <= 2147483647
            or not isinstance(self.action, str)
            or self.action not in GitLabMergeRequestAction
        ):
            raise ValueError("Invalid GitLab merge request command")


@dataclass(frozen=True)
class GitLabDiffRefs:
    """start_sha is the target snapshot; base_sha is the distinct merge base."""

    head_sha: str
    start_sha: str
    base_sha: str


@dataclass(frozen=True)
class CanonicalGitLabMergeRequest:
    target: BoundGitLabTarget
    external_id: int
    project_id: int
    iid: int
    target_project_id: int
    source_project_id: int
    state: GitLabMergeRequestState
    title: str
    description: str | None
    author_login: str
    web_url: str
    source_branch: str
    target_branch: str
    provider_updated_at: datetime
    diff_refs: GitLabDiffRefs


@dataclass(frozen=True)
class GitLabMergeRequestProjectionResult:
    target: BoundGitLabTarget
    code_change_id: UUID
    current_head: str
    state: GitLabMergeRequestState
    created: bool
    head_changed: bool


type GitLabMergeRequestCheckpoint = Callable[[], Awaitable[None]]


class CurrentGitLabMergeRequestProvider(Protocol):
    async def get_current(
        self,
        command: GitLabMergeRequestCommand,
        *,
        target: BoundGitLabTarget,
        checkpoint: GitLabMergeRequestCheckpoint,
    ) -> CanonicalGitLabMergeRequest: ...


class GitLabMergeRequestHandler(Protocol):
    async def execute(
        self, command: GitLabMergeRequestCommand
    ) -> GitLabMergeRequestProjectionResult | GitLabMergeRequestHandlingResult: ...


class GitLabMergeRequestIdentityConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_identity_conflict")


class CanonicalMergeRequestStale(ValueError):
    def __init__(self) -> None:
        super().__init__("gitlab_merge_request_stale")


@dataclass
class GitLabMergeRequestRecord:
    id: UUID
    head_sha: str
    provider_updated_at: datetime | None
    head_first_seen_at: datetime | None


@dataclass(frozen=True)
class LockedGitLabMergeRequest:
    record: GitLabMergeRequestRecord
    created: bool


class GitLabMergeRequestProjectionLock(Protocol):
    def hold(self, command: GitLabMergeRequestCommand) -> AbstractAsyncContextManager[None]: ...


class GitLabMergeRequestProjectionStore(Protocol):
    async def get_or_create_locked(
        self, current: CanonicalGitLabMergeRequest, now: datetime
    ) -> LockedGitLabMergeRequest: ...

    async def save(
        self, record: GitLabMergeRequestRecord, current: CanonicalGitLabMergeRequest
    ) -> None: ...


class GitLabMergeRequestRunCanceller(Protocol):
    async def cancel_for_merge_request(
        self,
        target: BoundGitLabTarget,
        code_change_id: UUID,
        reason: str,
        now: datetime,
        *,
        current_head: str,
    ) -> tuple[RunCancellationNotice, ...]: ...

    async def notify_run_updated(self, notice: RunCancellationNotice) -> None: ...


class GitLabMergeRequestProjectionUnitOfWork(UnitOfWork, Protocol):
    async def lock_target(self, target: BoundGitLabTarget) -> None: ...

    @property
    def merge_requests(self) -> GitLabMergeRequestProjectionStore: ...

    @property
    def runs(self) -> GitLabMergeRequestRunCanceller: ...


class ProjectGitLabMergeRequest:
    def __init__(
        self,
        *,
        authority: GitLabTargetReader,
        current_provider: CurrentGitLabMergeRequestProvider,
        projection_lock: GitLabMergeRequestProjectionLock,
        uow_factory: Callable[[], GitLabMergeRequestProjectionUnitOfWork],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._authority = authority
        self._current_provider = current_provider
        self._projection_lock = projection_lock
        self._uow_factory = uow_factory
        self._now = now

    async def execute(
        self, command: GitLabMergeRequestCommand
    ) -> GitLabMergeRequestProjectionResult:
        async with self._projection_lock.hold(command):
            target = await self._authority.resolve_target(
                GitLabTargetIdentity(command.origin_provider_installation_id, command.project_id)
            )
            current = await self._current_provider.get_current(
                command, target=target, checkpoint=partial(self._authority.checkpoint, target)
            )
            if (
                target.origin_provider_installation_id != command.origin_provider_installation_id
                or target.project_id != command.project_id
                or current.target != target
                or current.external_id != command.external_id
                or current.iid != command.iid
                or current.project_id != command.project_id
                or current.target_project_id != command.project_id
            ):
                raise CanonicalMergeRequestIdentityMismatch()
            now = self._now()
            async with self._uow_factory() as uow:
                await uow.lock_target(target)
                locked = await uow.merge_requests.get_or_create_locked(current, now)
                record = locked.record
                if (
                    record.provider_updated_at is not None
                    and current.provider_updated_at < record.provider_updated_at
                ):
                    raise CanonicalMergeRequestStale()
                changed = locked.created or record.head_sha != current.diff_refs.head_sha
                record.head_sha = current.diff_refs.head_sha
                record.provider_updated_at = current.provider_updated_at
                if changed:
                    record.head_first_seen_at = now
                await uow.merge_requests.save(record, current)
                reason = None
                if current.state != GitLabMergeRequestState.OPENED:
                    reason = "pr_closed"
                elif changed and not locked.created:
                    reason = "superseded"
                if reason is not None:
                    notices = await uow.runs.cancel_for_merge_request(
                        target, record.id, reason, now, current_head=current.diff_refs.head_sha
                    )
                    for notice in notices:
                        await uow.runs.notify_run_updated(notice)
                await uow.commit()
            return GitLabMergeRequestProjectionResult(
                target,
                record.id,
                current.diff_refs.head_sha,
                current.state,
                locked.created,
                changed,
            )


class GitLabMergeRequestProjector(Protocol):
    async def execute(
        self, command: GitLabMergeRequestCommand
    ) -> GitLabMergeRequestProjectionResult: ...


class GitLabRunEnqueuer(Protocol):
    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_identity: WebhookRunIdentity | None = None,
        triggered_by: str | None | UnsetType = UNSET,
    ) -> EnqueueResult: ...


@dataclass(frozen=True)
class GitLabMergeRequestHandlingResult:
    projection: GitLabMergeRequestProjectionResult
    enqueue: EnqueueResult | None


class HandleGitLabMergeRequest:
    def __init__(
        self, *, projector: GitLabMergeRequestProjector, enqueuer: GitLabRunEnqueuer
    ) -> None:
        self._projector, self._enqueuer = projector, enqueuer

    async def execute(self, command: GitLabMergeRequestCommand) -> GitLabMergeRequestHandlingResult:
        projection = await self._projector.execute(command)
        if (
            command.action
            not in {
                GitLabMergeRequestAction.OPEN,
                GitLabMergeRequestAction.REOPEN,
                GitLabMergeRequestAction.UPDATE,
            }
            or projection.state != GitLabMergeRequestState.OPENED
        ):
            return GitLabMergeRequestHandlingResult(projection, None)
        target = projection.target
        outcome = await self._enqueuer.execute(
            projection.code_change_id,
            projection.current_head,
            expected_identity=WebhookRunIdentity(
                "gitlab",
                target.origin_provider_installation_id,
                target.workspace_id,
                target.repository_id,
                target.project_id,
                target.instance_origin,
                target.credential_generation,
            ),
        )
        return GitLabMergeRequestHandlingResult(projection, outcome)
