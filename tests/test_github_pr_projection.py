"""PR state projection from durable GitHub webhook deliveries."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.pull_request_dtos import parse_pull_request_event
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    GitHubInstallationResolver,
    InstallationDeliveryDispatchStatus,
    InstallationOnboardingHandler,
)
from app.modules.integrations.webhooks.infrastructure.github_current_pull_request import (
    HttpGitHubCurrentPullRequestProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_reviewer_timeline import (
    HttpGitHubReviewerTimelineProvider,
)
from app.modules.reviews.application.project_github_pull_request import (
    LockedPullRequest,
    ProjectGitHubPullRequest,
    PullRequestEvent,
    PullRequestIdentityConflict,
    PullRequestLabelEvent,
    PullRequestProjectionStatus,
    PullRequestRecord,
    PullRequestState,
    ReviewerTimelineIntent,
    ReviewerTimelineSnapshot,
    RunCancellationNotice,
    TimelineLifecycle,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionLock,
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.models import CodeChange

_REPOSITORY_ID = UUID("11111111-1111-1111-1111-111111111111")
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_UPDATED = datetime(2026, 9, 28, 11, 59, tzinfo=UTC)
_HEAD = "a" * 40
_BASE = "b" * 40


def _event(action: str = "opened", **overrides: object) -> PullRequestEvent:
    values: dict[str, object] = {
        "action": action,
        "installation_external_id": 17,
        "repository_external_id": 101,
        "external_id": 901,
        "number": 7,
        "title": "Add parser",
        "description": "New parser",
        "author_login": "alice",
        "web_url": "https://github.com/octo/repo/pull/7",
        "source_branch": "feature/parser",
        "target_branch": "main",
        "base_sha": _BASE,
        "head_sha": _HEAD,
        "state": PullRequestState.OPEN,
        "provider_updated_at": _UPDATED,
        "repository_full_name": "octo/repo",
        "requested_reviewer_login": None,
        "sender_type": None,
    }
    values.update(overrides)
    return PullRequestEvent(**values)  # type: ignore[arg-type]


def _fake_uow_factory(uow: FakeUnitOfWork) -> Callable[[], FakeUnitOfWork]:
    return lambda: uow


@dataclass
class FakeStore:
    row: PullRequestRecord | None = None
    repository_id: UUID | None = _REPOSITORY_ID
    saves: int = 0

    async def get_or_create_locked(
        self, event: PullRequestEvent, now: datetime
    ) -> LockedPullRequest | None:
        if self.repository_id is None:
            return None
        created = self.row is None
        if self.row is None:
            self.row = PullRequestRecord.from_event(uuid4(), self.repository_id, event, now)
        return LockedPullRequest(self.row, created)

    async def save(self, record: PullRequestRecord) -> None:
        self.row = record
        self.saves += 1


@dataclass
class FakeUnitOfWork:
    store: FakeStore = field(default_factory=FakeStore)
    run_store: FakeRunCanceller = field(default_factory=lambda: FakeRunCanceller())
    commits: int = 0
    active: bool = False

    @property
    def pull_requests(self) -> FakeStore:
        return self.store

    @property
    def runs(self) -> FakeRunCanceller:
        return self.run_store

    async def __aenter__(self) -> Self:
        self.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


@dataclass
class FakeRunCanceller:
    calls: list[tuple[UUID, str]] = field(default_factory=list)
    notices: list[RunCancellationNotice] = field(default_factory=list)
    next_notices: tuple[RunCancellationNotice, ...] = ()

    async def cancel_for_pr(
        self, code_change_id: UUID, reason: str, now: datetime
    ) -> tuple[RunCancellationNotice, ...]:
        self.calls.append((code_change_id, reason))
        notices = self.next_notices
        self.next_notices = ()
        return notices

    async def notify_run_updated(self, notice: RunCancellationNotice) -> None:
        self.notices.append(notice)


@dataclass
class FakeProjectionLock:
    mutex: asyncio.Lock = field(default_factory=asyncio.Lock)

    @asynccontextmanager
    async def hold(self, event: PullRequestEvent) -> AsyncIterator[None]:
        async with self.mutex:
            yield


def test_opened_creates_metadata_and_starts_head_clock() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )

    result = asyncio.run(projector.execute(_event()))

    assert result == PullRequestProjectionStatus.PROJECTED
    assert uow.store.row is not None
    assert uow.store.row.external_number == 7
    assert uow.store.row.title == "Add parser"
    assert uow.store.row.author_login == "alice"
    assert uow.store.row.web_url == "https://github.com/octo/repo/pull/7"
    assert uow.store.row.source_branch == "feature/parser"
    assert uow.store.row.target_branch == "main"
    assert uow.store.row.base_sha == _BASE
    assert uow.store.row.head_sha == _HEAD
    assert uow.store.row.state == PullRequestState.OPEN
    assert uow.store.row.head_first_seen_at == _NOW
    assert uow.store.row.reviewer_requested_at is None
    assert uow.commits == 1


def test_bot_assignment_survives_push_and_only_new_head_resets_head_clock() -> None:
    uow = FakeUnitOfWork()
    clock = [_NOW]
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: clock[0]
    )
    asyncio.run(projector.execute(_event()))
    clock[0] = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    asyncio.run(
        projector.execute(
            _event(
                "review_requested",
                requested_reviewer_login="Reviewer[bot]",
                provider_updated_at=datetime(2026, 9, 28, 12, 1, tzinfo=UTC),
            )
        )
    )
    assert uow.store.row is not None
    uow.store.row.ci_status = {"head": "old"}
    clock[0] = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    asyncio.run(
        projector.execute(
            _event(
                "synchronize",
                title="Updated parser",
                provider_updated_at=datetime(2026, 9, 28, 12, 2, tzinfo=UTC),
            )
        )
    )

    assert uow.store.row.title == "Updated parser"
    assert uow.store.row.reviewer_requested is True
    assert uow.store.row.reviewer_requested_at == datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    assert uow.store.row.head_first_seen_at == _NOW
    assert uow.store.row.ci_status == {"head": "old"}

    clock[0] = datetime(2026, 9, 28, 12, 3, tzinfo=UTC)
    asyncio.run(
        projector.execute(
            _event(
                "synchronize",
                head_sha="c" * 40,
                base_sha="d" * 40,
                provider_updated_at=datetime(2026, 9, 28, 12, 3, tzinfo=UTC),
            )
        )
    )
    assert uow.store.row.head_sha == "c" * 40
    assert uow.store.row.base_sha == "d" * 40
    assert uow.store.row.head_first_seen_at == datetime(2026, 9, 28, 12, 3, tzinfo=UTC)
    assert uow.store.row.reviewer_requested is True
    assert uow.store.row.reviewer_requested_at == datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    assert uow.store.row.ci_status == {}


def test_duplicate_bot_request_preserves_first_assignment_time() -> None:
    uow = FakeUnitOfWork()
    clock = [_NOW]
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: clock[0]
    )
    asyncio.run(projector.execute(_event()))
    request = _event("review_requested", requested_reviewer_login="reviewer[bot]")
    asyncio.run(projector.execute(request))
    clock[0] = datetime(2026, 9, 28, 12, 5, tzinfo=UTC)
    asyncio.run(projector.execute(request))

    assert uow.store.row is not None
    assert uow.store.row.reviewer_requested_at == _NOW


def test_delayed_reviewer_intent_applies_across_new_head_in_both_orders() -> None:
    requested_at = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    removed_at = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    pushed_at = datetime(2026, 9, 28, 12, 3, tzinfo=UTC)
    request = _event(
        "review_requested",
        requested_reviewer_login="reviewer[bot]",
        provider_updated_at=requested_at,
    )
    remove = _event(
        "review_request_removed",
        requested_reviewer_login="reviewer[bot]",
        sender_type="User",
        provider_updated_at=removed_at,
    )
    push = _event("synchronize", head_sha="c" * 40, provider_updated_at=pushed_at)

    for actions in ((request, push), (push, request)):
        uow = FakeUnitOfWork()
        projector = ProjectGitHubPullRequest(
            uow_factory=_fake_uow_factory(uow), bot_login="reviewer[bot]", now=lambda: _NOW
        )
        asyncio.run(projector.execute(_event()))
        for action in actions:
            asyncio.run(projector.execute(action))
        assert uow.store.row is not None
        assert uow.store.row.head_sha == "c" * 40
        assert uow.store.row.reviewer_requested is True
        assert uow.store.row.reviewer_requested_at == _NOW

        asyncio.run(projector.execute(remove))
        assert uow.store.row.reviewer_requested is False
        assert uow.store.row.reviewer_requested_at is None

    for sequence in ((request, remove, push), (request, push, remove)):
        uow = FakeUnitOfWork()
        projector = ProjectGitHubPullRequest(
            uow_factory=_fake_uow_factory(uow), bot_login="reviewer[bot]", now=lambda: _NOW
        )
        asyncio.run(projector.execute(_event()))
        for action in sequence:
            asyncio.run(projector.execute(action))
        assert uow.store.row is not None
        assert uow.store.row.head_sha == "c" * 40
        assert uow.store.row.reviewer_requested is False


def test_head_change_and_close_cancel_runs_in_projection_transaction() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    asyncio.run(projector.execute(_event()))
    assert uow.store.row is not None
    notice = RunCancellationNotice(uuid4(), uuid4(), "cancelled")
    uow.run_store.next_notices = (notice,)
    asyncio.run(
        projector.execute(
            _event(
                "synchronize",
                head_sha="c" * 40,
                provider_updated_at=datetime(2026, 9, 28, 12, 1, tzinfo=UTC),
            )
        )
    )
    assert uow.run_store.calls == [(uow.store.row.id, "superseded")]
    assert uow.run_store.notices == [notice]

    asyncio.run(
        projector.execute(
            _event(
                "closed",
                head_sha="c" * 40,
                state=PullRequestState.CLOSED,
                provider_updated_at=datetime(2026, 9, 28, 12, 2, tzinfo=UTC),
            )
        )
    )
    assert uow.run_store.calls[-1] == (uow.store.row.id, "pr_closed")
    assert uow.run_store.notices == [notice]


def test_synchronize_and_close_publish_cancellation_ids_only_after_commit() -> None:
    uow = FakeUnitOfWork()

    @dataclass
    class Signals:
        calls: list[tuple[UUID, ...]] = field(default_factory=list)

        async def publish_for(self, run_ids: tuple[UUID, ...]) -> int:
            assert not uow.active
            assert uow.commits >= 2
            self.calls.append(run_ids)
            return len(run_ids)

    signals = Signals()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        cancellation_signals=signals,
        now=lambda: _NOW,
    )
    asyncio.run(projector.execute(_event()))
    first = RunCancellationNotice(UUID(int=1), UUID(int=2), "cancelled")
    uow.run_store.next_notices = (first,)
    synchronized = _event(
        "synchronize",
        head_sha="c" * 40,
        provider_updated_at=_NOW + timedelta(minutes=1),
    )
    assert asyncio.run(projector.execute(synchronized)) == PullRequestProjectionStatus.PROJECTED
    assert signals.calls == [(first.run_id,)]
    assert uow.run_store.notices == [first]

    assert asyncio.run(projector.execute(synchronized)) == PullRequestProjectionStatus.PROJECTED
    assert signals.calls == [(first.run_id,)]

    second = RunCancellationNotice(UUID(int=3), UUID(int=2), "cancelled")
    uow.run_store.next_notices = (second,)
    closed = _event(
        "closed",
        head_sha="c" * 40,
        state=PullRequestState.CLOSED,
        provider_updated_at=_NOW + timedelta(minutes=2),
    )
    assert asyncio.run(projector.execute(closed)) == PullRequestProjectionStatus.PROJECTED
    assert signals.calls == [(first.run_id,), (second.run_id,)]
    assert uow.run_store.notices == [first, second]


def test_close_barrier_and_equal_time_removal_keep_reviewer_intent_fail_closed() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    asyncio.run(projector.execute(_event()))
    intent_time = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    request = _event(
        "review_requested",
        requested_reviewer_login="reviewer[bot]",
        provider_updated_at=intent_time,
    )
    removal = _event(
        "review_request_removed",
        requested_reviewer_login="reviewer[bot]",
        sender_type="User",
        provider_updated_at=intent_time,
    )
    asyncio.run(projector.execute(request))
    asyncio.run(projector.execute(removal))
    asyncio.run(projector.execute(request))
    assert uow.store.row is not None
    assert uow.store.row.reviewer_requested is False

    reverse_uow = FakeUnitOfWork()
    reverse = ProjectGitHubPullRequest(
        uow_factory=lambda: reverse_uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    asyncio.run(reverse.execute(_event()))
    asyncio.run(reverse.execute(removal))
    asyncio.run(reverse.execute(request))
    assert reverse_uow.store.row is not None
    assert reverse_uow.store.row.reviewer_requested is False

    closed_at = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    asyncio.run(
        projector.execute(
            _event("closed", provider_updated_at=closed_at, state=PullRequestState.CLOSED)
        )
    )
    asyncio.run(projector.execute(_event("reopened", provider_updated_at=closed_at)))
    asyncio.run(projector.execute(request))
    assert uow.store.row.reviewer_requested is False

    later_request = _event(
        "review_requested",
        requested_reviewer_login="reviewer[bot]",
        provider_updated_at=datetime(2026, 9, 28, 12, 3, tzinfo=UTC),
    )
    asyncio.run(projector.execute(later_request))
    assert uow.store.row.reviewer_requested is True


def test_same_second_human_removal_then_fresh_request_uses_latest_timeline_event() -> None:
    same_time = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    remove = _event(
        "review_request_removed",
        requested_reviewer_login="reviewer[bot]",
        sender_type="User",
        provider_updated_at=same_time,
    )
    request = _event(
        "review_requested",
        requested_reviewer_login="reviewer[bot]",
        provider_updated_at=same_time,
    )

    @dataclass
    class Timeline:
        latest: ReviewerTimelineIntent

        async def snapshot(
            self, event: PullRequestEvent, bot_login: str
        ) -> ReviewerTimelineSnapshot:
            assert bot_login == "reviewer[bot]"
            return ReviewerTimelineSnapshot(self.latest, None)

    for delivery_order in ((remove, request), (request, remove)):
        uow = FakeUnitOfWork()
        timeline = Timeline(ReviewerTimelineIntent(20, same_time, requested=False, position=1))
        projector = ProjectGitHubPullRequest(
            uow_factory=_fake_uow_factory(uow),
            bot_login="reviewer[bot]",
            reviewer_timeline_provider=timeline,
            projection_lock=FakeProjectionLock(),
            now=lambda: _NOW,
        )
        asyncio.run(projector.execute(_event()))
        if delivery_order[0] == remove:
            asyncio.run(projector.execute(remove))
            timeline.latest = ReviewerTimelineIntent(11, same_time, requested=True, position=2)
            asyncio.run(projector.execute(request))
        else:
            timeline.latest = ReviewerTimelineIntent(11, same_time, requested=True, position=2)
            asyncio.run(projector.execute(request))
            asyncio.run(projector.execute(remove))
        assert uow.store.row is not None
        assert uow.store.row.reviewer_requested is True
        assert uow.store.row.reviewer_timeline_event_id == 11


def test_same_second_request_then_human_removal_uses_latest_timeline_event() -> None:
    same_time = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    request = _event(
        "review_requested",
        requested_reviewer_login="reviewer[bot]",
        provider_updated_at=same_time,
    )
    remove = _event(
        "review_request_removed",
        requested_reviewer_login="reviewer[bot]",
        sender_type="User",
        provider_updated_at=same_time,
    )

    @dataclass
    class Timeline:
        latest: ReviewerTimelineIntent

        async def snapshot(
            self, event: PullRequestEvent, bot_login: str
        ) -> ReviewerTimelineSnapshot:
            return ReviewerTimelineSnapshot(self.latest, None)

    for delivery_order in ((request, remove), (remove, request)):
        uow = FakeUnitOfWork()
        timeline = Timeline(ReviewerTimelineIntent(20, same_time, requested=True, position=1))
        projector = ProjectGitHubPullRequest(
            uow_factory=_fake_uow_factory(uow),
            bot_login="reviewer[bot]",
            reviewer_timeline_provider=timeline,
            projection_lock=FakeProjectionLock(),
            now=lambda: _NOW,
        )
        asyncio.run(projector.execute(_event()))
        if delivery_order[0] == request:
            asyncio.run(projector.execute(request))
            timeline.latest = ReviewerTimelineIntent(11, same_time, requested=False, position=2)
            asyncio.run(projector.execute(remove))
        else:
            timeline.latest = ReviewerTimelineIntent(11, same_time, requested=False, position=2)
            asyncio.run(projector.execute(remove))
            asyncio.run(projector.execute(request))
        assert uow.store.row is not None
        assert uow.store.row.reviewer_requested is False
        assert uow.store.row.reviewer_timeline_event_id == 11


def test_timeline_uncertainty_raises_without_committing_reviewer_intent() -> None:
    class UnavailableTimeline:
        async def snapshot(
            self, event: PullRequestEvent, bot_login: str
        ) -> ReviewerTimelineSnapshot:
            raise RuntimeError("GitHub timeline unavailable")

    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        reviewer_timeline_provider=UnavailableTimeline(),
        projection_lock=FakeProjectionLock(),
        now=lambda: _NOW,
    )
    asyncio.run(projector.execute(_event()))
    commits = uow.commits
    with pytest.raises(RuntimeError, match="timeline unavailable"):
        asyncio.run(
            projector.execute(_event("review_requested", requested_reviewer_login="reviewer[bot]"))
        )
    assert uow.commits == commits


def test_same_second_reopen_then_request_restores_flag_in_both_delivery_orders() -> None:
    same_time = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    closed = _event("closed", provider_updated_at=same_time, state=PullRequestState.CLOSED)
    reopened = _event("reopened", provider_updated_at=same_time)
    request = _event(
        "review_requested",
        provider_updated_at=same_time,
        requested_reviewer_login="reviewer[bot]",
    )
    snapshot = ReviewerTimelineSnapshot(
        intent=ReviewerTimelineIntent(13, same_time, requested=True, position=3),
        lifecycle=TimelineLifecycle(12, same_time, "reopened", position=2),
    )

    class Timeline:
        async def snapshot(
            self, event: PullRequestEvent, bot_login: str
        ) -> ReviewerTimelineSnapshot:
            return snapshot

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            return _event(event.action, provider_updated_at=same_time, state=PullRequestState.OPEN)

    for actions in ((closed, reopened, request), (request, closed, reopened)):
        uow = FakeUnitOfWork()
        projector = ProjectGitHubPullRequest(
            uow_factory=_fake_uow_factory(uow),
            bot_login="reviewer[bot]",
            current_provider=Current(),
            reviewer_timeline_provider=Timeline(),
            projection_lock=FakeProjectionLock(),
            now=lambda: _NOW,
        )
        asyncio.run(projector.execute(_event()))
        for action in actions:
            asyncio.run(projector.execute(action))
        assert uow.store.row is not None
        assert uow.store.row.state == PullRequestState.OPEN
        assert uow.store.row.reviewer_requested is True
        assert uow.store.row.reviewer_barrier_position == 2


def test_same_second_request_before_reopen_stays_cleared_in_both_delivery_orders() -> None:
    same_time = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    closed = _event("closed", provider_updated_at=same_time, state=PullRequestState.CLOSED)
    reopened = _event("reopened", provider_updated_at=same_time)
    request = _event(
        "review_requested",
        provider_updated_at=same_time,
        requested_reviewer_login="reviewer[bot]",
    )
    snapshot = ReviewerTimelineSnapshot(
        intent=ReviewerTimelineIntent(11, same_time, requested=True, position=1),
        lifecycle=TimelineLifecycle(13, same_time, "reopened", position=3),
    )

    class Timeline:
        async def snapshot(
            self, event: PullRequestEvent, bot_login: str
        ) -> ReviewerTimelineSnapshot:
            return snapshot

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            return _event(event.action, provider_updated_at=same_time, state=PullRequestState.OPEN)

    for actions in ((request, closed, reopened), (closed, reopened, request)):
        uow = FakeUnitOfWork()
        projector = ProjectGitHubPullRequest(
            uow_factory=_fake_uow_factory(uow),
            bot_login="reviewer[bot]",
            current_provider=Current(),
            reviewer_timeline_provider=Timeline(),
            projection_lock=FakeProjectionLock(),
            now=lambda: _NOW,
        )
        asyncio.run(projector.execute(_event()))
        for action in actions:
            asyncio.run(projector.execute(action))
        assert uow.store.row is not None
        assert uow.store.row.reviewer_requested is False


def test_reopen_before_delayed_close_clears_old_intent_but_preserves_new_request() -> None:
    uow = FakeUnitOfWork()
    closed_at = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    reopened_at = datetime(2026, 9, 28, 12, 3, tzinfo=UTC)

    class CurrentProvider:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            return _event(
                event.action, state=PullRequestState.OPEN, provider_updated_at=reopened_at
            )

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=CurrentProvider(),
        projection_lock=FakeProjectionLock(),
        now=lambda: _NOW,
    )
    asyncio.run(projector.execute(_event()))
    old_request = _event(
        "review_requested",
        requested_reviewer_login="reviewer[bot]",
        provider_updated_at=datetime(2026, 9, 28, 12, 1, tzinfo=UTC),
    )
    asyncio.run(projector.execute(old_request))
    asyncio.run(projector.execute(_event("reopened", provider_updated_at=reopened_at)))
    assert uow.store.row is not None
    assert uow.store.row.state == PullRequestState.OPEN
    assert uow.store.row.reviewer_requested is False

    asyncio.run(projector.execute(_event("closed", provider_updated_at=closed_at)))
    asyncio.run(projector.execute(old_request))
    assert uow.store.row.state == PullRequestState.OPEN
    assert uow.store.row.reviewer_requested is False

    fresh_request = _event(
        "review_requested",
        requested_reviewer_login="reviewer[bot]",
        provider_updated_at=datetime(2026, 9, 28, 12, 4, tzinfo=UTC),
    )
    asyncio.run(projector.execute(fresh_request))
    asyncio.run(projector.execute(_event("closed", provider_updated_at=closed_at)))
    assert uow.store.row.reviewer_requested is True


def test_equal_timestamp_pushes_reconcile_current_head_in_both_delivery_orders() -> None:
    head_b, head_c = "c" * 40, "d" * 40
    same_time = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)

    for delivered_heads in ((head_b, head_c), (head_c, head_b)):
        uow = FakeUnitOfWork()

        @dataclass
        class CurrentProvider:
            unit_of_work: FakeUnitOfWork
            calls: int = 0

            async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
                self.calls += 1
                assert not self.unit_of_work.active
                return _event(
                    event.action,
                    head_sha=head_c,
                    title="Current head",
                    provider_updated_at=same_time,
                )

        provider = CurrentProvider(uow)

        def uow_factory(unit_of_work: FakeUnitOfWork = uow) -> FakeUnitOfWork:
            return unit_of_work

        projector = ProjectGitHubPullRequest(
            uow_factory=uow_factory,
            bot_login="reviewer[bot]",
            current_provider=provider,
            projection_lock=FakeProjectionLock(),
            now=lambda: _NOW,
        )
        asyncio.run(projector.execute(_event()))
        for head in delivered_heads:
            asyncio.run(
                projector.execute(
                    _event("synchronize", head_sha=head, provider_updated_at=same_time)
                )
            )
        assert uow.store.row is not None
        assert uow.store.row.head_sha == head_c
        assert uow.store.row.title == "Current head"
        assert provider.calls == 2


def test_closed_new_head_then_delayed_sync_cannot_reopen_and_reopen_is_authoritative() -> None:
    uow = FakeUnitOfWork()
    head_b = "c" * 40
    same_time = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    current_state = [PullRequestState.CLOSED]

    @dataclass
    class CurrentProvider:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            return _event(
                event.action,
                head_sha=head_b,
                state=current_state[0],
                provider_updated_at=same_time,
            )

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=CurrentProvider(),
        projection_lock=FakeProjectionLock(),
        now=lambda: _NOW,
    )
    asyncio.run(projector.execute(_event()))
    asyncio.run(
        projector.execute(_event("review_requested", requested_reviewer_login="reviewer[bot]"))
    )
    asyncio.run(projector.execute(_event("closed", head_sha=head_b, state=PullRequestState.CLOSED)))
    assert uow.store.row is not None
    assert uow.store.row.head_sha == head_b
    assert uow.store.row.state == PullRequestState.CLOSED
    assert uow.store.row.reviewer_requested is False

    asyncio.run(
        projector.execute(_event("synchronize", head_sha=head_b, provider_updated_at=same_time))
    )
    assert uow.store.row.state == PullRequestState.CLOSED

    current_state[0] = PullRequestState.OPEN
    asyncio.run(
        projector.execute(_event("reopened", head_sha=head_b, provider_updated_at=same_time))
    )
    assert cast(PullRequestState, uow.store.row.state) == PullRequestState.OPEN
    asyncio.run(projector.execute(_event("closed", head_sha=head_b, provider_updated_at=same_time)))
    assert cast(PullRequestState, uow.store.row.state) == PullRequestState.OPEN


@pytest.mark.integration
def test_postgresql_pr_lock_serializes_connections_and_releases_after_use(
    legacy_pr_database: tuple[str, str, UUID],
) -> None:
    database_url, schema, _ = legacy_pr_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        first_lock = SqlAlchemyPullRequestProjectionLock(engine)
        second_lock = SqlAlchemyPullRequestProjectionLock(engine)
        second_entered = asyncio.Event()

        async def acquire_second() -> None:
            async with second_lock.hold(_event()):
                second_entered.set()

        try:
            async with first_lock.hold(_event()):
                second = asyncio.create_task(acquire_second())
                await asyncio.sleep(0.05)
                assert not second_entered.is_set()
            await asyncio.wait_for(second, timeout=2)
            assert second_entered.is_set()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgresql_first_edited_delivery_commits_and_equal_time_replay_is_stale(
    legacy_pr_database: tuple[str, str, UUID],
) -> None:
    database_url, schema, legacy_pr_id = legacy_pr_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        projector = ProjectGitHubPullRequest(
            uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(sessions),
            bot_login="reviewer[bot]",
            now=lambda: _NOW,
        )
        try:
            async with sessions.begin() as session:
                await session.execute(delete(CodeChange).where(CodeChange.id == legacy_pr_id))

            first = _event("edited", title="Latest metadata")
            assert await projector.execute(first) == PullRequestProjectionStatus.PROJECTED
            async with sessions() as session:
                rows = (await session.scalars(select(CodeChange))).all()
                assert len(rows) == 1
                assert rows[0].title == "Latest metadata"

            stale = _event("edited", title="Older metadata")
            assert await projector.execute(stale) == PullRequestProjectionStatus.IGNORED_STALE
            async with sessions() as session:
                rows = (await session.scalars(select(CodeChange))).all()
                assert len(rows) == 1
                assert rows[0].title == "Latest metadata"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgresql_label_deliveries_persist_add_remove_readd_timestamps(
    legacy_pr_database: tuple[str, str, UUID],
) -> None:
    database_url, schema, legacy_pr_id = legacy_pr_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        active_uow = [False]
        clock = [datetime(2026, 9, 28, 12, 1, tzinfo=UTC)]
        labels: set[str] = {"ai-review"}
        rest_calls: list[str] = []

        class TrackingUnitOfWork(SqlAlchemyPullRequestProjectionUnitOfWork):
            async def __aenter__(self) -> Self:
                assert not active_uow[0]
                entered = await super().__aenter__()
                active_uow[0] = True
                return entered

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                try:
                    await super().__aexit__(exc_type, exc, traceback)
                finally:
                    active_uow[0] = False

        class Current:
            async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
                assert not active_uow[0]
                rest_calls.append(event.action)
                return _event(event.action, current_label_names=frozenset(labels))

        projector = ProjectGitHubPullRequest(
            uow_factory=lambda: TrackingUnitOfWork(sessions),
            bot_login="reviewer[bot]",
            current_provider=Current(),
            projection_lock=FakeProjectionLock(),
            now=lambda: clock[0],
        )
        adapter = GitHubWebhookDispatchAdapter(
            GitHubInstallationDeliveryDispatcher(
                resolver=cast(GitHubInstallationResolver, None),
                onboarding=cast(InstallationOnboardingHandler, None),
                label_intent_projector=projector,
            )
        )

        async def deliver(action: str) -> InstallationDeliveryDispatchStatus:
            payload = _webhook_payload()
            payload["action"] = action
            payload["label"] = {"name": "ai-review"}
            payload.pop("requested_reviewer")
            result = await adapter.execute(
                VerifiedGitHubDelivery(
                    f"label-{action}-{clock[0]}", "pull_request", payload
                ).to_receipt()
            )
            return result.status

        async def stored() -> CodeChange:
            async with sessions() as session:
                rows = (await session.scalars(select(CodeChange))).all()
                assert len(rows) == 1
                session.expunge(rows[0])
                return rows[0]

        try:
            async with sessions.begin() as session:
                await session.execute(delete(CodeChange).where(CodeChange.id == legacy_pr_id))

            assert (
                await deliver("labeled") == InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
            )
            first = await stored()
            assert first.ai_review_labeled is True
            assert first.ai_review_labeled_at == clock[0]
            assert first.label_intent_updated_at == clock[0]
            assert first.head_first_seen_at == clock[0]

            labels.clear()
            clock[0] = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
            assert await deliver("unlabeled") == InstallationDeliveryDispatchStatus.PROJECTED_PR
            removed = await stored()
            assert removed.id == first.id
            assert removed.ai_review_labeled is False
            assert removed.ai_review_labeled_at is None
            assert removed.label_intent_updated_at == clock[0]
            assert removed.head_first_seen_at == first.head_first_seen_at

            labels.add("ai-review")
            clock[0] = datetime(2026, 9, 28, 12, 3, tzinfo=UTC)
            assert (
                await deliver("labeled") == InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
            )
            reapplied = await stored()
            assert reapplied.id == first.id
            assert reapplied.ai_review_labeled is True
            assert reapplied.ai_review_labeled_at == clock[0]
            assert reapplied.label_intent_updated_at == clock[0]
            assert reapplied.head_first_seen_at == first.head_first_seen_at
            assert rest_calls == ["labeled", "unlabeled", "labeled"]
            assert active_uow == [False]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_inverted_rest_observations_are_serialized_per_pr() -> None:
    uow = FakeUnitOfWork()
    current_head = ["c" * 40]
    same_time = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)

    async def exercise() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        provider_calls = 0

        class CurrentProvider:
            async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
                nonlocal provider_calls
                provider_calls += 1
                if event.head_sha == "c" * 40:
                    first_started.set()
                    await release_first.wait()
                assert not uow.active
                return _event(
                    event.action,
                    head_sha=current_head[0],
                    provider_updated_at=same_time,
                )

        projector = ProjectGitHubPullRequest(
            uow_factory=lambda: uow,
            bot_login="reviewer[bot]",
            current_provider=CurrentProvider(),
            projection_lock=FakeProjectionLock(),
            now=lambda: _NOW,
        )
        await projector.execute(_event())
        first = asyncio.create_task(projector.execute(_event("synchronize", head_sha="c" * 40)))
        await first_started.wait()
        second = asyncio.create_task(projector.execute(_event("synchronize", head_sha="d" * 40)))
        await asyncio.sleep(0.01)
        assert provider_calls == 1
        current_head[0] = "d" * 40
        release_first.set()
        await asyncio.gather(first, second)

        assert provider_calls == 2
        assert uow.store.row is not None
        assert uow.store.row.head_sha == "d" * 40

    asyncio.run(exercise())


def test_close_fetch_serializes_with_reviewer_request_and_keeps_closed_flag_clear() -> None:
    uow = FakeUnitOfWork()

    async def exercise() -> None:
        close_fetch_started = asyncio.Event()
        release_close_fetch = asyncio.Event()

        class CurrentProvider:
            async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
                close_fetch_started.set()
                await release_close_fetch.wait()
                return _event(
                    event.action,
                    state=PullRequestState.CLOSED,
                    provider_updated_at=datetime(2026, 9, 28, 12, 2, tzinfo=UTC),
                )

        projector = ProjectGitHubPullRequest(
            uow_factory=lambda: uow,
            bot_login="reviewer[bot]",
            current_provider=CurrentProvider(),
            projection_lock=FakeProjectionLock(),
            now=lambda: _NOW,
        )
        await projector.execute(_event())
        close = asyncio.create_task(
            projector.execute(_event("closed", state=PullRequestState.CLOSED))
        )
        await close_fetch_started.wait()
        request = asyncio.create_task(
            projector.execute(
                _event(
                    "review_requested",
                    requested_reviewer_login="reviewer[bot]",
                    provider_updated_at=datetime(2026, 9, 28, 12, 3, tzinfo=UTC),
                )
            )
        )
        await asyncio.sleep(0.01)
        assert uow.store.row is not None
        assert uow.store.row.reviewer_requested is False
        release_close_fetch.set()
        await asyncio.gather(close, request)
        assert uow.store.row.state == PullRequestState.CLOSED
        assert uow.store.row.reviewer_requested is False

    asyncio.run(exercise())


def test_closed_state_always_clears_reviewer_flag_even_if_intent_timestamp_is_later() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    asyncio.run(projector.execute(_event()))
    asyncio.run(
        projector.execute(
            _event(
                "review_requested",
                requested_reviewer_login="reviewer[bot]",
                provider_updated_at=datetime(2026, 9, 28, 12, 4, tzinfo=UTC),
            )
        )
    )
    asyncio.run(
        projector.execute(
            _event(
                "closed",
                state=PullRequestState.CLOSED,
                provider_updated_at=datetime(2026, 9, 28, 12, 2, tzinfo=UTC),
            )
        )
    )
    assert uow.store.row is not None
    assert uow.store.row.state == PullRequestState.CLOSED
    assert uow.store.row.reviewer_requested is False


def test_only_human_removal_of_our_bot_clears_assignment_and_close_clears_it() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    asyncio.run(projector.execute(_event()))
    asyncio.run(
        projector.execute(_event("review_requested", requested_reviewer_login="reviewer[bot]"))
    )
    assert uow.store.row is not None
    assert uow.store.row.reviewer_requested is True

    foreign = asyncio.run(
        projector.execute(
            _event(
                "review_request_removed",
                requested_reviewer_login="another-bot[bot]",
                sender_type="User",
            )
        )
    )
    bot_origin = asyncio.run(
        projector.execute(
            _event(
                "review_request_removed",
                requested_reviewer_login="reviewer[bot]",
                sender_type="Bot",
            )
        )
    )
    assert foreign == PullRequestProjectionStatus.IGNORED_UNRELATED
    assert bot_origin == PullRequestProjectionStatus.IGNORED_UNRELATED
    assert uow.store.row.reviewer_requested is True

    human = asyncio.run(
        projector.execute(
            _event(
                "review_request_removed",
                requested_reviewer_login="reviewer[bot]",
                sender_type="User",
            )
        )
    )
    assert human == PullRequestProjectionStatus.PROJECTED
    assert uow.store.row.reviewer_requested is False
    assert uow.store.row.reviewer_requested_at is None

    asyncio.run(
        projector.execute(_event("review_requested", requested_reviewer_login="reviewer[bot]"))
    )
    asyncio.run(projector.execute(_event("closed", state=PullRequestState.MERGED)))
    assert uow.store.row.state == PullRequestState.MERGED
    assert uow.store.row.reviewer_requested is False
    assert uow.store.row.reviewer_requested_at is None
    asyncio.run(projector.execute(_event("reopened", state=PullRequestState.OPEN)))
    assert uow.store.row.state == PullRequestState.OPEN


def test_stale_sha_unknown_repository_and_unrelated_pr_do_not_mutate_record() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    asyncio.run(projector.execute(_event()))
    assert uow.store.row is not None
    saves = uow.store.saves

    stale_push = asyncio.run(
        projector.execute(
            _event(
                "synchronize",
                head_sha="0" * 40,
                provider_updated_at=datetime(2026, 9, 28, 11, 58, tzinfo=UTC),
            )
        )
    )
    unrelated = asyncio.run(projector.execute(_event(external_id=902)))
    assert stale_push == PullRequestProjectionStatus.IGNORED_STALE
    assert unrelated == PullRequestProjectionStatus.IGNORED_UNRELATED
    assert uow.store.saves == saves
    assert uow.store.row.head_sha == _HEAD
    assert uow.store.row.reviewer_requested is False

    uow.store.repository_id = None
    unknown = asyncio.run(projector.execute(_event(repository_external_id=999)))
    assert unknown == PullRequestProjectionStatus.UNKNOWN_REPOSITORY
    assert uow.store.saves == saves


def test_external_id_collision_with_different_pr_number_is_unrelated() -> None:
    class CollidingStore(FakeStore):
        async def get_or_create_locked(
            self, event: PullRequestEvent, now: datetime
        ) -> LockedPullRequest | None:
            raise PullRequestIdentityConflict

    uow = FakeUnitOfWork(store=CollidingStore())
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )

    assert (
        asyncio.run(projector.execute(_event(number=8)))
        == PullRequestProjectionStatus.IGNORED_UNRELATED
    )
    assert uow.commits == 0


def _webhook_payload() -> dict[str, object]:
    return {
        "action": "review_requested",
        "number": 7,
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "requested_reviewer": {"login": "reviewer[bot]"},
        "sender": {"type": "User"},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Add parser",
            "body": "New parser",
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature/parser", "sha": _HEAD},
            "base": {"ref": "main", "sha": _BASE},
            "state": "open",
            "merged": False,
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }


def test_webhook_payload_parses_pr_metadata_and_bot_request() -> None:
    payload = _webhook_payload()

    event = parse_pull_request_event(payload)

    assert event == _event(
        "review_requested", requested_reviewer_login="reviewer[bot]", sender_type="User"
    )


def test_edited_receipt_updates_metadata_without_changing_intent_clock_or_enqueue() -> None:
    @dataclass
    class Trigger:
        prs: list[PullRequestEvent] = field(default_factory=list)
        ci: list[CiTriggerEvent] = field(default_factory=list)

        async def on_pr(self, event: PullRequestEvent) -> None:
            self.prs.append(event)

        async def on_ci(self, event: CiTriggerEvent) -> None:
            self.ci.append(event)

        async def on_label(self, event: PullRequestLabelEvent) -> None:
            raise AssertionError("edited must not enqueue")

    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    asyncio.run(projector.execute(_event()))
    assert uow.store.row is not None
    row = uow.store.row
    row.reviewer_requested = True
    row.reviewer_requested_at = _NOW
    row.ai_review_labeled = True
    row.ai_review_labeled_at = _NOW
    row.label_intent_updated_at = _NOW
    row.ci_status = {"head": "green"}
    first_seen = row.head_first_seen_at
    trigger = Trigger()
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
            pull_request_projector=projector,
            run_trigger=trigger,
        )
    )
    payload = _webhook_payload()
    payload["action"] = "edited"
    pr = dict(cast(Mapping[str, object], payload["pull_request"]))
    pr.update(
        {
            "title": "Updated title",
            "body": "Updated body",
            "user": {"login": "bob"},
            "head": {"ref": "feature/renamed", "sha": _HEAD},
            "base": {"ref": "release", "sha": "c" * 40},
            "updated_at": "2026-09-28T12:01:00Z",
        }
    )
    payload["pull_request"] = pr

    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery("delivery-edited", "pull_request", payload).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert (row.title, row.description, row.author_login) == (
        "Updated title",
        "Updated body",
        "bob",
    )
    assert (row.source_branch, row.target_branch, row.base_sha, row.head_sha) == (
        "feature/renamed",
        "release",
        "c" * 40,
        _HEAD,
    )
    # The persisted label intent arrives in Tasks 2b–2c; preserve existing intent meanwhile.
    assert (row.reviewer_requested, row.reviewer_requested_at) == (True, _NOW)
    assert (row.ai_review_labeled, row.ai_review_labeled_at, row.label_intent_updated_at) == (
        True,
        _NOW,
        _NOW,
    )
    assert row.head_first_seen_at == first_seen
    assert row.ci_status == {"head": "green"}
    assert trigger.prs == [] and trigger.ci == []
    saves = uow.store.saves
    stale_payload = dict(payload)
    stale_pr = dict(pr)
    stale_pr.update({"title": "Stale title", "updated_at": "2026-09-28T12:00:00Z"})
    stale_payload["pull_request"] = stale_pr

    stale = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery(
                "delivery-stale-edited", "pull_request", stale_payload
            ).to_receipt()
        )
    )

    assert stale.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert row.title == "Updated title"
    assert uow.store.saves == saves
    assert trigger.prs == [] and trigger.ci == []

    same_second_pr = dict(pr)
    same_second_pr["title"] = "Older title with same timestamp"
    same_second_payload = dict(payload)
    same_second_payload["pull_request"] = same_second_pr
    same_second = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery(
                "delivery-same-second-edited", "pull_request", same_second_payload
            ).to_receipt()
        )
    )

    assert same_second.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert row.title == "Updated title"
    assert uow.store.saves == saves
    assert trigger.prs == [] and trigger.ci == []


def test_first_edited_delivery_creates_pr_without_current_provider() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
            pull_request_projector=projector,
        )
    )
    payload = _webhook_payload()
    payload["action"] = "edited"
    pr = dict(cast(Mapping[str, object], payload["pull_request"]))
    pr["title"] = "First observed metadata"
    payload["pull_request"] = pr

    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery("first-edited", "pull_request", payload).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert uow.store.row is not None
    assert uow.store.row.title == "First observed metadata"
    assert uow.store.saves == 1
    assert uow.commits == 1


def test_old_reviewer_intent_does_not_opt_in_to_new_label_state() -> None:
    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow, bot_login="reviewer[bot]", now=lambda: _NOW
    )
    assert asyncio.run(projector.execute(_event())) == PullRequestProjectionStatus.PROJECTED
    assert uow.store.row is not None
    row = uow.store.row
    row.reviewer_requested = True
    row.reviewer_requested_at = _NOW
    first_seen = row.head_first_seen_at

    result = asyncio.run(
        projector.execute(
            _event(
                "edited",
                title="Updated metadata",
                provider_updated_at=datetime(2026, 9, 28, 12, 1, tzinfo=UTC),
            )
        )
    )

    assert result == PullRequestProjectionStatus.PROJECTED
    assert row.title == "Updated metadata"
    assert row.reviewer_requested is True
    assert row.ai_review_labeled is False
    assert row.ai_review_labeled_at is None
    assert row.label_intent_updated_at is None
    assert row.head_first_seen_at == first_seen


def test_label_projection_reconciles_add_remove_readd_and_delayed_deliveries() -> None:
    uow = FakeUnitOfWork()
    clock = [datetime(2026, 9, 28, 12, 1, tzinfo=UTC)]
    labels: set[str] = set()
    observations: list[str] = []

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            observations.append(event.action)
            return _event(
                event.action,
                provider_updated_at=datetime(2026, 9, 28, 12, 1, tzinfo=UTC),
                current_label_names=frozenset(labels),
            )

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        now=lambda: clock[0],
    )
    assert asyncio.run(projector.execute(_event())) == PullRequestProjectionStatus.PROJECTED
    assert uow.store.row is not None
    row = uow.store.row
    row.reviewer_requested = True  # Historical state cannot opt in by itself.
    row.ci_status = {"head": "green"}
    first_seen = row.head_first_seen_at
    add = PullRequestLabelEvent(_event("labeled"), "ai-review")
    remove = PullRequestLabelEvent(_event("unlabeled"), "ai-review")
    unrelated = PullRequestLabelEvent(_event("labeled"), "docs")

    assert (
        asyncio.run(projector.execute(unrelated)) == PullRequestProjectionStatus.IGNORED_UNRELATED
    )
    assert observations == []
    assert row.ai_review_labeled is False
    labels.add("ai-review")
    assert asyncio.run(projector.execute(add)) == PullRequestProjectionStatus.PROJECTED
    assert (row.ai_review_labeled, row.ai_review_labeled_at) == (True, clock[0])
    activation = row.ai_review_labeled_at
    clock[0] = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    assert asyncio.run(projector.execute(add)) == PullRequestProjectionStatus.PROJECTED
    assert asyncio.run(projector.execute(remove)) == PullRequestProjectionStatus.PROJECTED
    assert (
        row.ai_review_labeled_at == activation
    )  # Duplicate and delayed removal see current label.

    labels.clear()
    clock[0] = datetime(2026, 9, 28, 12, 3, tzinfo=UTC)
    assert asyncio.run(projector.execute(remove)) == PullRequestProjectionStatus.PROJECTED
    assert row.ai_review_labeled is False
    assert row.ai_review_labeled_at is None
    assert row.label_intent_updated_at == clock[0]
    assert asyncio.run(projector.execute(add)) == PullRequestProjectionStatus.PROJECTED
    assert row.ai_review_labeled is False  # Delayed add cannot defeat current removal.

    labels.add("ai-review")
    clock[0] = datetime(2026, 9, 28, 12, 4, tzinfo=UTC)
    assert asyncio.run(projector.execute(add)) == PullRequestProjectionStatus.PROJECTED
    assert row.ai_review_labeled is True
    assert row.ai_review_labeled_at == clock[0]
    assert row.label_intent_updated_at == clock[0]
    assert row.reviewer_requested is True
    assert row.head_first_seen_at == first_seen
    assert row.ci_status == {"head": "green"}
    assert uow.run_store.calls == []
    assert observations == ["labeled", "labeled", "unlabeled", "unlabeled", "labeled", "labeled"]


def test_human_unlabels_ai_review_without_cancelling_active_run_or_enqueuing() -> None:
    uow = FakeUnitOfWork()
    labels = {"ai-review"}

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            return _event(event.action, current_label_names=frozenset(labels))

    @dataclass
    class Signals:
        published: list[tuple[UUID, ...]] = field(default_factory=list)

        async def publish_for(self, run_ids: tuple[UUID, ...]) -> int:
            self.published.append(run_ids)
            return len(run_ids)

    @dataclass
    class Trigger:
        calls: list[str] = field(default_factory=list)

        async def on_pr(self, event: PullRequestEvent) -> None:
            self.calls.append(event.action)

        async def on_label(self, event: PullRequestLabelEvent) -> None:
            self.calls.append(event.pull_request.action)

        async def on_ci(self, event: CiTriggerEvent) -> None:
            self.calls.append("ci")

    signals = Signals()
    trigger = Trigger()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        cancellation_signals=signals,
        now=lambda: _NOW,
    )
    assert asyncio.run(projector.execute(_event())) == PullRequestProjectionStatus.PROJECTED
    assert uow.store.row is not None
    row = uow.store.row
    assert (
        asyncio.run(projector.execute(PullRequestLabelEvent(_event("labeled"), "ai-review")))
        == PullRequestProjectionStatus.PROJECTED
    )
    assert row.ai_review_labeled is True
    active_run = RunCancellationNotice(uuid4(), uuid4(), "cancelled")
    uow.run_store.next_notices = (active_run,)
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
            label_intent_projector=projector,
            run_trigger=trigger,
        )
    )
    payload = _webhook_payload()
    payload["action"] = "unlabeled"
    payload["label"] = {"name": "ai-review"}
    payload["sender"] = {"type": "User", "login": "alice"}
    payload.pop("requested_reviewer")
    labels.clear()

    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery("human-unlabeled", "pull_request", payload).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert row.ai_review_labeled is False
    assert row.ai_review_labeled_at is None
    assert row.label_intent_updated_at == _NOW
    assert uow.run_store.calls == []
    assert uow.run_store.notices == []
    assert uow.run_store.next_notices == (active_run,)
    assert signals.published == []
    assert trigger.calls == []


@pytest.mark.parametrize("action,initially_labeled", [("labeled", False), ("unlabeled", True)])
def test_self_bot_label_delivery_is_ignored_before_projection_and_trigger(
    action: str, initially_labeled: bool
) -> None:
    uow = FakeUnitOfWork()
    provider_calls: list[str] = []
    trigger_calls: list[str] = []

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            provider_calls.append(event.action)
            return _event(event.action, current_label_names=frozenset())

    class Trigger:
        async def on_pr(self, event: PullRequestEvent) -> None:
            trigger_calls.append(event.action)

        async def on_label(self, event: PullRequestLabelEvent) -> None:
            trigger_calls.append(event.pull_request.action)

        async def on_ci(self, event: CiTriggerEvent) -> None:
            trigger_calls.append("ci")

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        now=lambda: _NOW,
    )
    assert asyncio.run(projector.execute(_event())) == PullRequestProjectionStatus.PROJECTED
    assert uow.store.row is not None
    row = uow.store.row
    row.ai_review_labeled = initially_labeled
    row.ai_review_labeled_at = _NOW if initially_labeled else None
    saves = uow.store.saves
    commits = uow.commits
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
            label_intent_projector=projector,
            run_trigger=Trigger(),
        )
    )
    payload = _webhook_payload()
    payload["action"] = action
    payload["label"] = {"name": "ai-review"}
    payload["sender"] = {"type": "Bot", "login": "Reviewer[bot]"}
    payload.pop("requested_reviewer")

    result = asyncio.run(
        adapter.execute(
            VerifiedGitHubDelivery(f"bot-{action}", "pull_request", payload).to_receipt()
        )
    )

    assert result.status is InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
    assert row.ai_review_labeled is initially_labeled
    assert row.ai_review_labeled_at == (_NOW if initially_labeled else None)
    assert uow.store.saves == saves
    assert uow.commits == commits
    assert uow.run_store.calls == []
    assert provider_calls == []
    assert trigger_calls == []


def test_delayed_label_projects_current_head_and_cancels_obsolete_run() -> None:
    uow = FakeUnitOfWork()
    current_head = "c" * 40
    current_at = datetime(2026, 9, 28, 12, 5, tzinfo=UTC)

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            return _event(
                event.action,
                head_sha=current_head,
                base_sha="d" * 40,
                provider_updated_at=current_at,
                current_label_names=frozenset({"ai-review"}),
            )

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        now=lambda: current_at,
    )
    asyncio.run(projector.execute(_event()))
    assert uow.store.row is not None
    row = uow.store.row
    row.ci_status = {"old": "success"}

    result = asyncio.run(projector.execute(PullRequestLabelEvent(_event("labeled"), "ai-review")))

    assert result == PullRequestProjectionStatus.PROJECTED
    assert row.head_sha == current_head
    assert row.base_sha == "d" * 40
    assert row.head_first_seen_at == current_at
    assert row.ci_status == {}
    assert row.ai_review_labeled is True
    assert uow.run_store.calls == [(row.id, "superseded")]


def test_delayed_label_projects_current_closed_lifecycle() -> None:
    uow = FakeUnitOfWork()
    current_at = datetime(2026, 9, 28, 12, 5, tzinfo=UTC)

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            return _event(
                event.action,
                state=PullRequestState.CLOSED,
                provider_updated_at=current_at,
                current_label_names=frozenset({"ai-review"}),
            )

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        now=lambda: current_at,
    )
    asyncio.run(projector.execute(_event()))
    assert uow.store.row is not None
    row = uow.store.row

    result = asyncio.run(projector.execute(PullRequestLabelEvent(_event("labeled"), "ai-review")))

    assert result == PullRequestProjectionStatus.PROJECTED
    assert row.state == PullRequestState.CLOSED
    assert row.ai_review_labeled is True
    assert uow.run_store.calls == [(row.id, "pr_closed")]


def test_current_label_reconciles_old_row_on_sync_and_survives_close_reopen() -> None:
    uow = FakeUnitOfWork()
    clock = [datetime(2026, 9, 28, 12, 1, tzinfo=UTC)]
    state = [PullRequestState.OPEN]
    head = ["c" * 40]

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            return _event(
                event.action,
                state=state[0],
                head_sha=head[0],
                provider_updated_at=clock[0],
                current_label_names=frozenset({"ai-review"}),
            )

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        now=lambda: clock[0],
    )
    asyncio.run(projector.execute(_event()))
    assert uow.store.row is not None
    row = uow.store.row
    row.reviewer_requested = True  # Simulates an old migrated row.
    row.ci_status = {"head": "old"}
    assert row.ai_review_labeled is False

    assert (
        asyncio.run(projector.execute(_event("synchronize")))
        == PullRequestProjectionStatus.PROJECTED
    )
    assert row.head_sha == "c" * 40
    assert row.head_first_seen_at == clock[0]
    assert row.ci_status == {}
    assert (row.ai_review_labeled, row.ai_review_labeled_at) == (True, clock[0])
    activation = row.ai_review_labeled_at

    state[0] = PullRequestState.CLOSED
    clock[0] = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
    assert (
        asyncio.run(projector.execute(_event("closed", state=PullRequestState.CLOSED)))
        == PullRequestProjectionStatus.PROJECTED
    )
    assert row.state == PullRequestState.CLOSED
    assert (row.ai_review_labeled, row.ai_review_labeled_at) == (True, activation)

    state[0] = PullRequestState.OPEN
    clock[0] = datetime(2026, 9, 28, 12, 3, tzinfo=UTC)
    assert (
        asyncio.run(projector.execute(_event("reopened"))) == PullRequestProjectionStatus.PROJECTED
    )
    assert cast(PullRequestState, row.state) == PullRequestState.OPEN
    assert (row.ai_review_labeled, row.ai_review_labeled_at) == (True, activation)


def test_first_label_receipt_projects_through_typed_adapter_and_real_intent_projector() -> None:
    uow = FakeUnitOfWork()

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            return _event(event.action, current_label_names=frozenset({"ai-review"}))

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        now=lambda: _NOW,
    )
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
            label_intent_projector=projector,
        )
    )
    payload = _webhook_payload()
    payload["action"] = "labeled"
    payload["label"] = {"name": "ai-review"}
    payload.pop("requested_reviewer")

    result = asyncio.run(
        adapter.execute(VerifiedGitHubDelivery("first-label", "pull_request", payload).to_receipt())
    )

    assert result.status is InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
    assert uow.store.row is not None
    assert uow.store.row.ai_review_labeled is True
    assert uow.store.row.ai_review_labeled_at == _NOW
    assert uow.store.row.label_intent_updated_at == _NOW
    assert uow.commits == 1


@pytest.mark.parametrize("titles", [("New title", "Old title"), ("Old title", "New title")])
def test_same_second_edited_replay_uses_current_metadata_in_either_order(
    titles: tuple[str, str],
) -> None:
    edited_at = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
    uow = FakeUnitOfWork()
    calls: list[str] = []

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert not uow.active
            calls.append(event.title)
            return _event(
                "edited",
                title="New title",
                description="Newest body",
                author_login="bob",
                source_branch="feature/latest",
                target_branch="release",
                base_sha="c" * 40,
                head_sha="d" * 40,
                provider_updated_at=edited_at,
            )

    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=FakeProjectionLock(),
        now=lambda: _NOW,
    )
    assert asyncio.run(projector.execute(_event())) == PullRequestProjectionStatus.PROJECTED
    assert uow.store.row is not None
    row = uow.store.row
    row.reviewer_requested = True
    row.reviewer_requested_at = _NOW
    row.ci_status = {"head": "green"}
    first_seen = row.head_first_seen_at

    for title in titles:
        assert (
            asyncio.run(
                projector.execute(_event("edited", title=title, provider_updated_at=edited_at))
            )
            == PullRequestProjectionStatus.PROJECTED
        )

    assert calls == list(titles)
    assert (row.title, row.description, row.author_login) == (
        "New title",
        "Newest body",
        "bob",
    )
    assert (row.source_branch, row.target_branch, row.base_sha) == (
        "feature/latest",
        "release",
        "c" * 40,
    )
    assert (row.reviewer_requested, row.reviewer_requested_at) == (True, _NOW)
    assert row.head_first_seen_at == first_seen
    assert row.head_sha == _HEAD
    assert row.ci_status == {"head": "green"}
    assert uow.run_store.calls == []


def test_current_pr_provider_fetches_authoritative_snapshot_with_installation_token() -> None:
    seen: list[httpx.Request] = []

    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 17
            return "installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        current = dict(cast(Mapping[str, object], _webhook_payload()["pull_request"]))
        current["head"] = {"ref": "feature/parser", "sha": "c" * 40}
        current["state"] = "closed"
        current["labels"] = [{"name": "ai-review"}, {"name": "docs"}]
        return httpx.Response(200, json=current)

    async def exercise() -> PullRequestEvent:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = HttpGitHubCurrentPullRequestProvider(client=client, token_provider=Tokens())
            return await provider.get_current(_event("synchronize"))

    result = asyncio.run(exercise())

    assert result.head_sha == "c" * 40
    assert result.current_label_names == frozenset({"ai-review", "docs"})
    assert result.state == PullRequestState.CLOSED
    assert len(seen) == 1
    assert seen[0].url.path == "/repos/octo/repo/pulls/7"
    assert seen[0].headers["Authorization"] == "Bearer installation-token"


@pytest.mark.parametrize("labels", [None, [{"name": 7}], [{"name": ""}]])
def test_current_pr_without_valid_labels_cannot_clear_label_state(labels: object) -> None:
    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        current = dict(cast(Mapping[str, object], _webhook_payload()["pull_request"]))
        if labels is not None:
            current["labels"] = labels
        return httpx.Response(200, json=current)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = HttpGitHubCurrentPullRequestProvider(client=client, token_provider=Tokens())
            await provider.get_current(_event("synchronize"))

    with pytest.raises(ValueError):
        asyncio.run(exercise())


def test_reviewer_timeline_paginates_and_ignores_automatic_bot_removal() -> None:
    seen: list[httpx.Request] = []

    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 17
            return "installation-token"

    def reviewer_event(
        event_id: int, action: str, *, actor_type: str = "User"
    ) -> dict[str, object]:
        return {
            "id": event_id,
            "event": action,
            "created_at": "2026-09-28T12:01:00Z",
            "actor": {"type": actor_type},
            "requested_reviewer": {"login": "reviewer[bot]"},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"event": "committed"}] * 100)
        return httpx.Response(
            200,
            json=[
                reviewer_event(10, "review_request_removed"),
                reviewer_event(11, "review_requested"),
                reviewer_event(12, "review_request_removed", actor_type="Bot"),
            ],
        )

    async def exercise() -> ReviewerTimelineSnapshot:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = HttpGitHubReviewerTimelineProvider(client=client, token_provider=Tokens())
            return await provider.snapshot(_event("review_requested"), "reviewer[bot]")

    result = asyncio.run(exercise())

    assert result == ReviewerTimelineSnapshot(
        ReviewerTimelineIntent(
            11, datetime(2026, 9, 28, 12, 1, tzinfo=UTC), requested=True, position=102
        ),
        None,
    )
    assert len(seen) == 2
    assert seen[1].url.path == "/repos/octo/repo/issues/7/timeline"
    assert seen[1].url.params["per_page"] == "100"
    assert seen[1].headers["Authorization"] == "Bearer installation-token"


def test_reviewer_timeline_invalid_or_missing_intent_fails_for_receipt_retry() -> None:
    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "installation-token"

    async def exercise(items: object) -> ReviewerTimelineSnapshot:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=items)),
            base_url="https://api.github.com",
        ) as client:
            provider = HttpGitHubReviewerTimelineProvider(client=client, token_provider=Tokens())
            return await provider.snapshot(_event("review_requested"), "reviewer[bot]")

    for items in (
        [],
        [{"event": "review_requested", "requested_reviewer": {"login": "reviewer[bot]"}}],
        {"message": "incomplete"},
    ):
        with pytest.raises(ValueError):
            asyncio.run(exercise(items))


def test_reviewer_timeline_orders_same_second_lifecycle_and_request() -> None:
    class Tokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            return "installation-token"

    entries = [
        {"id": 20, "event": "closed", "created_at": "2026-09-28T12:02:00Z"},
        {"id": 19, "event": "reopened", "created_at": "2026-09-28T12:02:00Z"},
        {
            "id": 18,
            "event": "review_requested",
            "created_at": "2026-09-28T12:02:00Z",
            "requested_reviewer": {"login": "reviewer[bot]"},
            "actor": {"type": "User"},
        },
    ]

    async def exercise() -> ReviewerTimelineSnapshot:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=entries)),
            base_url="https://api.github.com",
        ) as client:
            provider = HttpGitHubReviewerTimelineProvider(client=client, token_provider=Tokens())
            return await provider.snapshot(_event("review_requested"), "reviewer[bot]")

    result = asyncio.run(exercise())
    assert result.lifecycle == TimelineLifecycle(
        19, datetime(2026, 9, 28, 12, 2, tzinfo=UTC), "reopened", position=2
    )
    assert result.intent == ReviewerTimelineIntent(
        18, datetime(2026, 9, 28, 12, 2, tzinfo=UTC), requested=True, position=3
    )


def test_missing_lifecycle_timeline_does_not_commit_close() -> None:
    class IncompleteTimeline:
        async def snapshot(
            self, event: PullRequestEvent, bot_login: str
        ) -> ReviewerTimelineSnapshot:
            return ReviewerTimelineSnapshot(None, None)

    uow = FakeUnitOfWork()
    projector = ProjectGitHubPullRequest(
        uow_factory=lambda: uow,
        bot_login="reviewer[bot]",
        reviewer_timeline_provider=IncompleteTimeline(),
        projection_lock=FakeProjectionLock(),
        now=lambda: _NOW,
    )
    asyncio.run(projector.execute(_event()))
    commits = uow.commits
    with pytest.raises(ValueError, match="lifecycle"):
        asyncio.run(projector.execute(_event("closed", state=PullRequestState.CLOSED)))
    assert uow.commits == commits


def test_dispatcher_projects_reopened_pr_event_from_durable_delivery() -> None:
    @dataclass
    class Projector:
        events: list[PullRequestEvent] = field(default_factory=list)

        async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
            self.events.append(event)
            return PullRequestProjectionStatus.PROJECTED

    projector = Projector()

    @dataclass
    class Trigger:
        prs: list[PullRequestEvent] = field(default_factory=list)
        ci: list[CiTriggerEvent] = field(default_factory=list)

        async def on_pr(self, event: PullRequestEvent) -> None:
            self.prs.append(event)

        async def on_ci(self, event: CiTriggerEvent) -> None:
            self.ci.append(event)

        async def on_label(self, event: PullRequestLabelEvent) -> None:
            raise AssertionError("reopened is not a label event")

    trigger = Trigger()
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=cast(GitHubInstallationResolver, None),
        onboarding=cast(InstallationOnboardingHandler, None),
        pull_request_projector=projector,
        run_trigger=trigger,
    )
    payload = _webhook_payload()
    payload["action"] = "reopened"
    payload.pop("requested_reviewer")
    delivery = VerifiedGitHubDelivery("delivery-pr", "pull_request", payload)

    result = asyncio.run(GitHubWebhookDispatchAdapter(dispatcher).execute(delivery.to_receipt()))

    assert result.status == InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert projector.events == [_event("reopened", sender_type="User")]
    assert trigger.prs == projector.events

    ci_delivery = VerifiedGitHubDelivery(
        "delivery-ci",
        "check_suite",
        {
            "action": "completed",
            "installation": {"id": 17},
            "repository": {"id": 101},
            "check_suite": {"head_sha": _HEAD},
        },
    )
    assert (
        asyncio.run(
            GitHubWebhookDispatchAdapter(dispatcher).execute(ci_delivery.to_receipt())
        ).status
        == InstallationDeliveryDispatchStatus.PROCESSED_CI
    )
    assert trigger.ci == [CiTriggerEvent(17, 101, _HEAD)]


@pytest.mark.parametrize("action", ["opened", "edited"])
@pytest.mark.parametrize("trigger_configured", [False, True])
def test_pr_metadata_delivery_projects_without_triggering_run(
    action: str, trigger_configured: bool
) -> None:
    @dataclass
    class Projector:
        events: list[PullRequestEvent] = field(default_factory=list)

        async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
            self.events.append(event)
            return PullRequestProjectionStatus.PROJECTED

    @dataclass
    class Trigger:
        prs: list[PullRequestEvent] = field(default_factory=list)

        async def on_pr(self, event: PullRequestEvent) -> None:
            self.prs.append(event)

        async def on_ci(self, event: CiTriggerEvent) -> None:
            pass

        async def on_label(self, event: PullRequestLabelEvent) -> None:
            raise AssertionError("metadata must not enqueue")

    projector = Projector()
    trigger = Trigger()
    payload = _webhook_payload()
    payload["action"] = action
    payload.pop("requested_reviewer")
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=cast(GitHubInstallationResolver, None),
        onboarding=cast(InstallationOnboardingHandler, None),
        pull_request_projector=projector,
        run_trigger=trigger if trigger_configured else None,
    )

    result = asyncio.run(
        GitHubWebhookDispatchAdapter(dispatcher).execute(
            VerifiedGitHubDelivery("delivery-pr", "pull_request", payload).to_receipt()
        )
    )

    assert result.status == InstallationDeliveryDispatchStatus.PROJECTED_PR
    assert projector.events == [_event(action, sender_type="User")]
    assert trigger.prs == []


def test_pr_delivery_remains_retryable_until_confirmed_publisher_is_configured() -> None:
    @dataclass
    class Projector:
        async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
            return PullRequestProjectionStatus.PROJECTED

    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=cast(GitHubInstallationResolver, None),
        onboarding=cast(InstallationOnboardingHandler, None),
        pull_request_projector=Projector(),
    )
    payload = _webhook_payload()
    payload["action"] = "reopened"
    payload.pop("requested_reviewer")
    status = asyncio.run(
        GitHubWebhookDispatchAdapter(dispatcher).execute(
            VerifiedGitHubDelivery("delivery-pr", "pull_request", payload).to_receipt()
        )
    ).status
    assert status == InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT


@pytest.fixture
def legacy_pr_database() -> Iterator[tuple[str, str, UUID]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_pr_projection_{uuid4().hex}"
    workspace_id, installation_id, repository_id, legacy_pr_id = (uuid4() for _ in range(4))
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "20260928_0013")
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, daily_budget_usd) "
                    "VALUES (:id, 'PR projection test', 0)"
                ),
                {"id": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :workspace_id, 'github', 17, '{}'::jsonb)"
                ),
                {"id": installation_id, "workspace_id": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO repositories "
                    "(id, provider_installation_id, external_id, full_name, "
                    "default_branch, web_url) "
                    "VALUES (:id, :installation_id, 101, 'octo/repo', 'main', "
                    "'https://github.com/octo/repo')"
                ),
                {"id": repository_id, "installation_id": installation_id},
            )
            connection.execute(
                text(
                    "INSERT INTO code_changes "
                    "(id, repository_id, external_id, external_number, title, "
                    "source_branch, target_branch, base_sha, head_sha, state, web_url) "
                    "VALUES (:id, :repository_id, 901, 7, 'Legacy title', "
                    "'feature/old', 'main', :base_sha, :head_sha, 'open', "
                    "'https://github.com/octo/repo/pull/7')"
                ),
                {
                    "id": legacy_pr_id,
                    "repository_id": repository_id,
                    "base_sha": _BASE,
                    "head_sha": _HEAD,
                },
            )
            connection.commit()
            command.upgrade(config, "head")
            yield database_url, schema, legacy_pr_id
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_migration_backfills_legacy_pr_from_event_and_duplicate_keeps_clocks(
    legacy_pr_database: tuple[str, str, UUID],
) -> None:
    database_url, schema, legacy_pr_id = legacy_pr_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        first_seen = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
        clock = [first_seen]
        projector = ProjectGitHubPullRequest(
            uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(sessions),
            bot_login="reviewer[bot]",
            now=lambda: clock[0],
        )
        try:
            async with sessions() as session:
                legacy = await session.get(CodeChange, legacy_pr_id)
                assert legacy is not None
                assert legacy.author_login is None
                assert legacy.head_first_seen_at is None
                assert legacy.reviewer_requested_at is None
                assert legacy.provider_updated_at is None
                assert legacy.reviewer_intent_updated_at is None
                assert legacy.reviewer_barrier_at is None

            assert await projector.execute(_event()) == PullRequestProjectionStatus.PROJECTED
            async with sessions() as session:
                row = await session.get(CodeChange, legacy_pr_id)
                assert row is not None
                assert row.author_login == "alice"
                assert row.head_first_seen_at == first_seen
                assert row.provider_updated_at == _UPDATED

            clock[0] = datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
            request = _event(
                "review_requested",
                requested_reviewer_login="reviewer[bot]",
                provider_updated_at=clock[0],
            )
            assert await projector.execute(request) == PullRequestProjectionStatus.PROJECTED
            clock[0] = datetime(2026, 9, 28, 12, 2, tzinfo=UTC)
            assert await projector.execute(request) == PullRequestProjectionStatus.PROJECTED
            async with sessions() as session:
                row = await session.get(CodeChange, legacy_pr_id)
                assert row is not None
                assert row.reviewer_requested is True
                assert row.reviewer_requested_at == datetime(2026, 9, 28, 12, 1, tzinfo=UTC)
                assert row.head_first_seen_at == first_seen

            assert (
                await projector.execute(
                    _event("synchronize", head_sha="0" * 40, provider_updated_at=_UPDATED)
                )
                == PullRequestProjectionStatus.IGNORED_STALE
            )
            assert (
                await projector.execute(_event(repository_external_id=999))
                == PullRequestProjectionStatus.UNKNOWN_REPOSITORY
            )
            assert (
                await projector.execute(_event(number=8))
                == PullRequestProjectionStatus.IGNORED_UNRELATED
            )
            async with sessions() as session:
                rows = (await session.scalars(select(CodeChange))).all()
                assert len(rows) == 1
                assert rows[0].head_sha == _HEAD

        finally:
            await engine.dispose()

    asyncio.run(exercise())
