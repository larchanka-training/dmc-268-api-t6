"""Post-commit T6 RunGuard pointers and durable broker retry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self, cast
from uuid import UUID

from app.modules.reviews.application.publish_cancellation_signals import (
    PublishCancellationSignals,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EligibilityChecker,
    PendingRunMessage,
    RunPublicationKind,
    TryEnqueueWebhookRun,
    WebhookRunUnitOfWork,
)

_RUN = UUID(int=1)
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _message() -> PendingRunMessage:
    return PendingRunMessage(
        run_id=_RUN,
        workspace_id=UUID(int=2),
        installation_id=17,
        repository_id=UUID(int=3),
        repository_external_id=101,
        repository_full_name="octo/repo",
        pr_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        engine="fast",
        rule_version_id=UUID(int=4),
        prompt_version_id=UUID(int=5),
        attempt=3,
        requested_at=_NOW,
    )


@dataclass
class Store:
    message: PendingRunMessage = field(default_factory=_message)
    published: bool = False
    normal_pending: bool = False
    normal_published: bool = False

    async def pending_cancellation_signals(
        self, limit: int, run_ids: tuple[UUID, ...] | None = None
    ) -> tuple[PendingRunMessage, ...]:
        if self.published or (run_ids is not None and self.message.run_id not in run_ids):
            return ()
        return (self.message,)

    async def mark_cancellation_signal_published(self, run_id: UUID, now: datetime) -> None:
        assert run_id == _RUN and now == _NOW
        self.published = True

    async def pending_messages(self, limit: int) -> tuple[PendingRunMessage, ...]:
        return (self.message,) if self.normal_pending and not self.normal_published else ()

    async def mark_published(self, run_id: UUID, now: datetime) -> None:
        assert run_id == _RUN and now == _NOW
        self.normal_published = True


@dataclass
class Uow:
    store: Store = field(default_factory=Store)
    active: bool = False
    commits: int = 0

    @property
    def runs(self) -> Store:
        return self.store

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
class Publisher:
    uow: Uow
    fail: bool = True
    calls: list[tuple[PendingRunMessage, RunPublicationKind]] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert not self.uow.active
        self.calls.append((message, kind))
        if self.fail:
            raise RuntimeError("broker confirm unavailable")


def test_failed_post_commit_cancellation_signal_remains_replayable() -> None:
    uow = Uow()
    publisher = Publisher(uow)
    use_case = PublishCancellationSignals(
        uow_factory=lambda: uow, publisher=publisher, now=lambda: _NOW
    )

    assert asyncio.run(use_case.publish_for((_RUN,))) == 0
    assert not uow.store.published
    assert uow.commits == 0
    publisher.fail = False
    assert asyncio.run(use_case.replay_pending()) == 1
    assert uow.store.published
    assert uow.commits == 1
    assert publisher.calls == [
        (_message(), RunPublicationKind.CANCELLATION),
        (_message(), RunPublicationKind.CANCELLATION),
    ]
    assert asyncio.run(use_case.replay_pending()) == 0


def test_existing_worker_publication_sweep_replays_cancellation_signals() -> None:
    uow = Uow()
    publisher = Publisher(uow, fail=False)
    cancellation = PublishCancellationSignals(
        uow_factory=lambda: uow, publisher=publisher, now=lambda: _NOW
    )
    trigger = TryEnqueueWebhookRun(
        eligibility=cast(EligibilityChecker, None),
        uow_factory=lambda: cast(WebhookRunUnitOfWork, uow),
        publisher=publisher,
        cancellation_signals=cancellation,
    )

    assert asyncio.run(trigger.replay_pending_publications()) == 1
    assert publisher.calls == [(_message(), RunPublicationKind.CANCELLATION)]


def test_publication_sweep_sends_urgent_cancellation_before_ordinary_run() -> None:
    uow = Uow(store=Store(normal_pending=True))
    publisher = Publisher(uow, fail=False)
    cancellation = PublishCancellationSignals(
        uow_factory=lambda: uow, publisher=publisher, now=lambda: _NOW
    )
    trigger = TryEnqueueWebhookRun(
        eligibility=cast(EligibilityChecker, None),
        uow_factory=lambda: cast(WebhookRunUnitOfWork, uow),
        publisher=publisher,
        cancellation_signals=cancellation,
        now=lambda: _NOW,
    )

    assert asyncio.run(trigger.replay_pending_publications()) == 2
    assert [kind for _, kind in publisher.calls] == [
        RunPublicationKind.CANCELLATION,
        RunPublicationKind.QUEUED,
    ]


def test_cancellation_replay_failure_does_not_block_ordinary_publication() -> None:
    class FailingCancellation:
        async def replay_pending(self, *, limit: int = 100) -> int:
            raise RuntimeError("cancellation store unavailable")

    uow = Uow(store=Store(normal_pending=True))
    publisher = Publisher(uow, fail=False)
    trigger = TryEnqueueWebhookRun(
        eligibility=cast(EligibilityChecker, None),
        uow_factory=lambda: cast(WebhookRunUnitOfWork, uow),
        publisher=publisher,
        cancellation_signals=FailingCancellation(),
        now=lambda: _NOW,
    )

    assert asyncio.run(trigger.replay_pending_publications()) == 1
    assert publisher.calls == [(_message(), RunPublicationKind.QUEUED)]
