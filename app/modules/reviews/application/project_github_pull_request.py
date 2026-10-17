"""Project GitHub pull request deliveries into durable PR and CI-clock state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork


class PullRequestState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class PullRequestProjectionStatus(StrEnum):
    PROJECTED = "projected"
    UNKNOWN_REPOSITORY = "unknown_repository"
    # The event's installation stores the repository, but it is disabled.
    DISABLED_REPOSITORY = "disabled_repository"
    # The repository is stored only under another installation.
    OTHER_INSTALLATION_REPOSITORY = "other_installation_repository"
    IGNORED_STALE = "ignored_stale"
    # Only paths the dispatcher already filters (review requests, unhandled actions).
    IGNORED_UNRELATED = "ignored_unrelated"
    # The label event was sent by the App's own bot.
    IGNORED_OWN_BOT = "ignored_own_bot"
    # The current GitHub PR is not the PR the event names (id, number, repository, installation).
    IGNORED_IDENTITY_MISMATCH = "ignored_identity_mismatch"
    # The PR id and number identify different stored PRs.
    IGNORED_IDENTITY_CONFLICT = "ignored_identity_conflict"
    # The stored PR with this number has another provider id.
    IGNORED_EXTERNAL_ID_MISMATCH = "ignored_external_id_mismatch"
    # A label other than ai-review.
    IGNORED_OTHER_LABEL = "ignored_other_label"


class PullRequestIdentityConflict(Exception):
    """A provider PR ID and number identify different persisted PR rows."""


class PullRequestRepositoryRefused(Exception):
    """The repository is stored, but disabled or only under another installation."""

    def __init__(self, status: PullRequestProjectionStatus) -> None:
        super().__init__(status.value)
        self.status = status


@dataclass(frozen=True)
class PullRequestEvent:
    action: str
    installation_external_id: int
    repository_external_id: int
    external_id: int
    number: int
    title: str
    description: str | None
    author_login: str
    web_url: str
    source_branch: str
    target_branch: str
    base_sha: str
    head_sha: str
    state: PullRequestState
    provider_updated_at: datetime
    repository_full_name: str | None = None
    sender_type: str | None = None
    sender_login: str | None = None
    current_label_names: frozenset[str] | None = None


@dataclass(frozen=True)
class PullRequestLabelEvent:
    pull_request: PullRequestEvent
    label_name: str


@dataclass
class PullRequestRecord:
    id: UUID
    repository_id: UUID
    external_id: int
    external_number: int
    title: str
    description: str | None
    author_login: str | None
    web_url: str
    source_branch: str
    target_branch: str
    base_sha: str
    head_sha: str
    state: PullRequestState
    ai_review_labeled: bool = False
    ai_review_labeled_at: datetime | None = None
    label_intent_updated_at: datetime | None = None
    reviewer_requested: bool = False
    reviewer_requested_at: datetime | None = None
    reviewer_intent_updated_at: datetime | None = None
    reviewer_timeline_event_id: int | None = None
    reviewer_timeline_position: int | None = None
    reviewer_barrier_at: datetime | None = None
    reviewer_barrier_position: int | None = None
    head_first_seen_at: datetime | None = None
    provider_updated_at: datetime | None = None
    ci_status: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_event(
        cls, id: UUID, repository_id: UUID, event: PullRequestEvent, now: datetime
    ) -> PullRequestRecord:
        return cls(
            id=id,
            repository_id=repository_id,
            external_id=event.external_id,
            external_number=event.number,
            title=event.title,
            description=event.description,
            author_login=event.author_login,
            web_url=event.web_url,
            source_branch=event.source_branch,
            target_branch=event.target_branch,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            state=event.state,
            head_first_seen_at=now,
            provider_updated_at=event.provider_updated_at,
        )


@dataclass(frozen=True)
class LockedPullRequest:
    record: PullRequestRecord
    created: bool


class PullRequestProjectionStore(Protocol):
    async def lock_active_installation(self, event: PullRequestEvent) -> bool:
        """Lock source; return false if unknown, raise repository refusal if retired."""
        ...

    async def get_or_create_locked(
        self, event: PullRequestEvent, now: datetime
    ) -> LockedPullRequest | None: ...

    async def save(self, record: PullRequestRecord) -> None: ...


@dataclass(frozen=True)
class RunCancellationNotice:
    run_id: UUID
    workspace_id: UUID
    status: str


class PullRequestRunCanceller(Protocol):
    async def cancel_for_pr(
        self, code_change_id: UUID, reason: str, now: datetime
    ) -> tuple[RunCancellationNotice, ...]: ...

    async def notify_run_updated(self, notice: RunCancellationNotice) -> None: ...


class PullRequestProjectionUnitOfWork(UnitOfWork, Protocol):
    @property
    def pull_requests(self) -> PullRequestProjectionStore: ...

    @property
    def runs(self) -> PullRequestRunCanceller: ...


type PullRequestProviderCheckpoint = Callable[[], Awaitable[None]]


class CurrentPullRequestProvider(Protocol):
    """Fetch a current provider snapshot before opening the projection transaction."""

    async def get_current(
        self, event: PullRequestEvent, *, checkpoint: PullRequestProviderCheckpoint
    ) -> PullRequestEvent: ...


class PullRequestProjectionLock(Protocol):
    """Serialize REST observation and its database application for one PR."""

    def hold(self, event: PullRequestEvent) -> AbstractAsyncContextManager[None]: ...


class CancellationSignalPublisher(Protocol):
    async def publish_for(self, run_ids: tuple[UUID, ...]) -> int: ...


class ProjectGitHubPullRequest:
    """Apply one verified PR event and cancel work made obsolete by its state."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], PullRequestProjectionUnitOfWork],
        bot_login: str,
        current_provider: CurrentPullRequestProvider | None = None,
        projection_lock: PullRequestProjectionLock | None = None,
        cancellation_signals: CancellationSignalPublisher | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not bot_login:
            raise ValueError("GitHub bot login is required")
        if current_provider is not None and projection_lock is None:
            raise ValueError("GitHub providers require a projection lock")
        self._uow_factory = uow_factory
        self._bot_login = bot_login.casefold()
        self._current_provider = current_provider
        self._projection_lock = projection_lock
        self._cancellation_signals = cancellation_signals
        self._now = now

    async def execute(
        self, event: PullRequestEvent | PullRequestLabelEvent
    ) -> PullRequestProjectionStatus:
        if isinstance(event, PullRequestLabelEvent):
            if event.label_name != "ai-review":
                return PullRequestProjectionStatus.IGNORED_OTHER_LABEL
            if (
                event.pull_request.sender_type == "Bot"
                and event.pull_request.sender_login is not None
                and event.pull_request.sender_login.casefold() == self._bot_login
            ):
                return PullRequestProjectionStatus.IGNORED_OWN_BOT
            if self._projection_lock is None or self._current_provider is None:
                raise RuntimeError(
                    "label projection requires current GitHub PR and projection lock"
                )
            async with self._projection_lock.hold(event.pull_request):
                return await self._project_label_under_lock(event.pull_request)
        if event.action in {"review_requested", "review_request_removed"}:
            return PullRequestProjectionStatus.IGNORED_UNRELATED

        if self._projection_lock is not None:
            async with self._projection_lock.hold(event):
                return await self._execute_under_lock(event)
        return await self._execute_under_lock(event)

    async def _project_label_under_lock(
        self, event: PullRequestEvent
    ) -> PullRequestProjectionStatus:
        source_refusal = await self._source_refusal(event)
        if source_refusal is not None:
            return source_refusal
        assert self._current_provider is not None
        try:
            current = await self._current_provider.get_current(
                event, checkpoint=partial(self._checkpoint, event)
            )
        except PullRequestRepositoryRefused as refused:
            return refused.status
        if not self._same_identity(current, event):
            return PullRequestProjectionStatus.IGNORED_IDENTITY_MISMATCH
        if current.current_label_names is None:
            raise ValueError("current GitHub PR response has no labels")
        now = self._now()
        async with self._uow_factory() as uow:
            try:
                locked = await uow.pull_requests.get_or_create_locked(current, now)
            except PullRequestIdentityConflict:
                return PullRequestProjectionStatus.IGNORED_IDENTITY_CONFLICT
            except PullRequestRepositoryRefused as refused:
                return refused.status
            if locked is None:
                return PullRequestProjectionStatus.UNKNOWN_REPOSITORY
            record = locked.record
            if record.external_id != current.external_id:
                return PullRequestProjectionStatus.IGNORED_EXTERNAL_ID_MISMATCH
            previous_head_sha = record.head_sha
            if record.provider_updated_at is None or (
                current.provider_updated_at >= record.provider_updated_at
            ):
                if current.head_sha != record.head_sha:
                    self._apply_new_head(record, current, now)
                self._apply_metadata(record, current, authoritative=True, now=now)
                record.provider_updated_at = current.provider_updated_at
            self._reconcile_label_state(record, current.current_label_names, now)
            await uow.pull_requests.save(record)
            notices = await self._cancel_obsolete_runs(uow, record, current, previous_head_sha, now)
            await uow.commit()
        if notices and self._cancellation_signals is not None:
            await self._cancellation_signals.publish_for(tuple(notice.run_id for notice in notices))
        return PullRequestProjectionStatus.PROJECTED

    @staticmethod
    def _same_identity(current: PullRequestEvent, event: PullRequestEvent) -> bool:
        return (
            current.external_id == event.external_id
            and current.number == event.number
            and current.repository_external_id == event.repository_external_id
            and current.installation_external_id == event.installation_external_id
        )

    async def _execute_under_lock(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
        source_refusal = await self._source_refusal(event)
        if source_refusal is not None:
            return source_refusal
        authoritative = self._current_provider is not None and event.action in {
            "synchronize",
            "closed",
            "reopened",
            "edited",
        }
        if authoritative:
            source_refusal = await self._source_refusal(event)
            if source_refusal is not None:
                return source_refusal
            assert self._current_provider is not None
            try:
                current = await self._current_provider.get_current(
                    event, checkpoint=partial(self._checkpoint, event)
                )
            except PullRequestRepositoryRefused as refused:
                return refused.status
            if not self._same_identity(current, event):
                return PullRequestProjectionStatus.IGNORED_IDENTITY_MISMATCH
            return await self._project(
                current,
                authoritative=True,
            )
        return await self._project(
            event,
            authoritative=False,
        )

    async def _checkpoint(self, event: PullRequestEvent) -> None:
        refusal = await self._source_refusal(event)
        if refusal is not None:
            raise PullRequestRepositoryRefused(refusal)

    async def _source_refusal(self, event: PullRequestEvent) -> PullRequestProjectionStatus | None:
        # The UOW releases its installation lock before any GitHub provider call.
        async with self._uow_factory() as uow:
            try:
                active = await uow.pull_requests.lock_active_installation(event)
            except PullRequestRepositoryRefused as refused:
                return refused.status
            return None if active else PullRequestProjectionStatus.UNKNOWN_REPOSITORY

    async def _project(
        self,
        event: PullRequestEvent,
        *,
        authoritative: bool,
    ) -> PullRequestProjectionStatus:
        now = self._now()
        async with self._uow_factory() as uow:
            try:
                locked = await uow.pull_requests.get_or_create_locked(event, now)
            except PullRequestIdentityConflict:
                return PullRequestProjectionStatus.IGNORED_IDENTITY_CONFLICT
            except PullRequestRepositoryRefused as refused:
                return refused.status
            if locked is None:
                return PullRequestProjectionStatus.UNKNOWN_REPOSITORY
            record = locked.record
            if record.external_id != event.external_id:
                return PullRequestProjectionStatus.IGNORED_EXTERNAL_ID_MISMATCH
            previous_head_sha = record.head_sha
            if self._is_stale_opened(record, event):
                return PullRequestProjectionStatus.IGNORED_STALE
            if record.provider_updated_at is not None and (
                event.provider_updated_at < record.provider_updated_at
                or (
                    event.action == "edited"
                    and not authoritative
                    and not locked.created
                    and event.provider_updated_at == record.provider_updated_at
                )
            ):
                return PullRequestProjectionStatus.IGNORED_STALE
            if event.action != "edited" and event.head_sha != record.head_sha:
                if self._head_change_is_stale(record, event, authoritative=authoritative):
                    return PullRequestProjectionStatus.IGNORED_STALE
                self._apply_new_head(record, event, now)

            if event.action in {"opened", "synchronize", "edited"}:
                self._apply_metadata(record, event, authoritative=authoritative, now=now)
            elif event.action in {"closed", "reopened"}:
                self._apply_lifecycle(record, event, authoritative=authoritative)
            else:
                # Unreachable behind the dispatcher, which passes only handled actions: generic.
                return PullRequestProjectionStatus.IGNORED_UNRELATED

            if (
                authoritative
                and event.action in {"synchronize", "closed", "reopened"}
                and event.current_label_names is not None
            ):
                self._reconcile_label_state(record, event.current_label_names, now)
            record.provider_updated_at = event.provider_updated_at
            await uow.pull_requests.save(record)
            notices = await self._cancel_obsolete_runs(uow, record, event, previous_head_sha, now)
            await uow.commit()
        if notices and self._cancellation_signals is not None:
            await self._cancellation_signals.publish_for(tuple(notice.run_id for notice in notices))
        return PullRequestProjectionStatus.PROJECTED

    @staticmethod
    def _reconcile_label_state(
        record: PullRequestRecord, current_label_names: frozenset[str], now: datetime
    ) -> None:
        labeled = "ai-review" in current_label_names
        if labeled == record.ai_review_labeled and (not labeled or record.ai_review_labeled_at):
            return
        record.ai_review_labeled = labeled
        record.ai_review_labeled_at = now if labeled else None
        record.label_intent_updated_at = now
        record.ci_status = {}

    @staticmethod
    def _is_stale_opened(record: PullRequestRecord, event: PullRequestEvent) -> bool:
        return (
            event.action == "opened"
            and record.state != PullRequestState.OPEN
            and record.provider_updated_at is not None
            and event.provider_updated_at <= record.provider_updated_at
        )

    @staticmethod
    def _head_change_is_stale(
        record: PullRequestRecord, event: PullRequestEvent, *, authoritative: bool
    ) -> bool:
        return (event.action not in {"opened", "synchronize"} and not authoritative) or (
            record.provider_updated_at is not None
            and event.provider_updated_at <= record.provider_updated_at
            and not (authoritative and event.provider_updated_at == record.provider_updated_at)
        )

    @staticmethod
    def _apply_new_head(record: PullRequestRecord, event: PullRequestEvent, now: datetime) -> None:
        record.head_sha = event.head_sha
        record.head_first_seen_at = now
        record.ci_status = {}

    @staticmethod
    def _apply_metadata(
        record: PullRequestRecord,
        event: PullRequestEvent,
        *,
        authoritative: bool,
        now: datetime,
    ) -> None:
        record.title = event.title
        record.description = event.description
        record.author_login = event.author_login
        record.web_url = event.web_url
        record.source_branch = event.source_branch
        record.target_branch = event.target_branch
        record.base_sha = event.base_sha
        if event.action != "edited":
            record.state = event.state if authoritative else PullRequestState.OPEN
        if record.head_first_seen_at is None and event.action != "edited":
            record.head_first_seen_at = now

    @staticmethod
    def _apply_lifecycle(
        record: PullRequestRecord, event: PullRequestEvent, *, authoritative: bool
    ) -> None:
        record.state = (
            event.state if event.action == "closed" or authoritative else PullRequestState.OPEN
        )

    @staticmethod
    async def _cancel_obsolete_runs(
        uow: PullRequestProjectionUnitOfWork,
        record: PullRequestRecord,
        event: PullRequestEvent,
        previous_head_sha: str,
        now: datetime,
    ) -> tuple[RunCancellationNotice, ...]:
        cancellation_reason: str | None = None
        if event.action in {"synchronize", "closed", "reopened", "labeled", "unlabeled"}:
            if record.state != PullRequestState.OPEN:
                cancellation_reason = "pr_closed"
            elif record.head_sha != previous_head_sha:
                cancellation_reason = "superseded"
        if cancellation_reason is not None:
            notices = await uow.runs.cancel_for_pr(record.id, cancellation_reason, now)
            for notice in notices:
                await uow.runs.notify_run_updated(notice)
            return notices
        return ()
