"""In-process delivery of durable review-run lifecycle updates."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_EVENT_ID = re.compile(r"[0-9]{1,19}")


@dataclass(frozen=True)
class RunUpdated:
    """A lifecycle state that has already been durably persisted."""

    run_id: UUID
    status: str


@dataclass(frozen=True)
class RunStep:
    """One persisted pipeline step, delivered independently of run status."""

    run_id: UUID
    tool: str
    index: int
    ok: bool
    started_at: datetime
    duration_ms: int
    attempt: int
    llm_operation: str | None = None


def utc_rfc3339(value: datetime) -> str:
    """UTC timestamp with a ``Z`` suffix, shared by ``NOTIFY`` payloads and SSE frames."""
    text = value.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return f"{text[:-6]}Z"
    return text


class RunUpdatePublisher(Protocol):
    async def publish(self, event: RunUpdated) -> None: ...


class RunUpdateStream(RunUpdatePublisher, Protocol):
    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[RunUpdated]]: ...


class RunStepStream(Protocol):
    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[RunStep]]: ...


@dataclass(frozen=True)
class RunChange:
    """The current state of a visible Run and the `updated_at` that names its SSE event."""

    run_id: UUID
    status: str
    updated_at: datetime


class RunAccessRepository(Protocol):
    async def run_updated_at(self, run_id: UUID) -> datetime | None:
        """`updated_at` of a Run the caller may see; `None` when it is not visible."""
        ...

    async def runs_updated_after(self, after: datetime, limit: int) -> list[RunChange]:
        """Visible Runs with `updated_at > after`, oldest first; the newest `limit` on overflow."""
        ...


def run_event_id(updated_at: datetime) -> str:
    """The SSE `id`: `updated_at` as whole microseconds since the Unix epoch (integer math)."""
    return str((updated_at - _EPOCH) // _MICROSECOND)


def parse_run_event_id(value: str | None) -> datetime | None:
    """The `updated_at` a `Last-Event-ID` names, or `None` when it is not a plain decimal id."""
    if value is None or _EVENT_ID.fullmatch(value) is None:
        return None
    try:
        return _EPOCH + timedelta(microseconds=int(value))
    except OverflowError:
        return None


class _Subscriber:
    def __init__(self) -> None:
        self.pending: dict[UUID, RunUpdated] = {}
        self.ready = asyncio.Event()


class InMemoryRunUpdateHub:
    """Fan out events to live clients, coalescing a slow client's updates per run.

    A subscriber keeps only the latest status of each run it has not read yet, so
    several changes of one run collapse to the last one and an event of another run
    never displaces it.
    """

    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: RunUpdated) -> None:
        for subscriber in tuple(self._subscribers):
            # Re-inserting moves the run to the end: runs are delivered in update order.
            subscriber.pending.pop(event.run_id, None)
            subscriber.pending[event.run_id] = event
            subscriber.ready.set()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[RunUpdated]]:
        subscriber = _Subscriber()
        self._subscribers.add(subscriber)

        async def events() -> AsyncIterator[RunUpdated]:
            while True:
                while not subscriber.pending:
                    subscriber.ready.clear()
                    await subscriber.ready.wait()
                yield subscriber.pending.pop(next(iter(subscriber.pending)))

        try:
            yield events()
        finally:
            self._subscribers.discard(subscriber)


class _StepSubscriber:
    def __init__(self) -> None:
        self.pending: list[RunStep] = []
        self.ready = asyncio.Event()


class InMemoryRunStepHub:
    """Fan out pipeline steps without coalescing.

    Each subscriber keeps a FIFO list. Two steps of one run are both delivered, in
    order. A later step does not replace an unread one, and a step never shares the
    status hub's per-run slot.
    """

    def __init__(self) -> None:
        self._subscribers: set[_StepSubscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: RunStep) -> None:
        for subscriber in tuple(self._subscribers):
            subscriber.pending.append(event)
            subscriber.ready.set()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[RunStep]]:
        subscriber = _StepSubscriber()
        self._subscribers.add(subscriber)

        async def events() -> AsyncIterator[RunStep]:
            while True:
                while not subscriber.pending:
                    subscriber.ready.clear()
                    await subscriber.ready.wait()
                yield subscriber.pending.pop(0)

        try:
            yield events()
        finally:
            self._subscribers.discard(subscriber)


@dataclass(frozen=True)
class StreamKeepalive:
    """A quiet interval on the live stream. The transport sends a comment frame."""


async def _visible_updated_at(repository: RunAccessRepository, run_id: UUID) -> datetime | None:
    if not hasattr(repository, "run_updated_at"):
        raise AttributeError(f"{type(repository).__name__} does not implement run_updated_at")
    return await repository.run_updated_at(run_id)


class _HubClosed:
    """The live iterator for one hub ended."""


_HUB_CLOSED = _HubClosed()


async def _replay_status(
    repository: RunAccessRepository,
    *,
    since: datetime | None,
    expires_at: int | None,
    now: Callable[[], float],
    replay_overlap: timedelta,
    replay_limit: int,
) -> AsyncIterator[RunChange]:
    """Last-Event-ID replay is status only. Steps are not replayed."""
    if since is None or (expires_at is not None and now() >= expires_at):
        return
    after = since - replay_overlap
    for change in await repository.runs_updated_after(after, replay_limit):
        yield change


async def _live_status(
    task: asyncio.Task[RunUpdated], repository: RunAccessRepository
) -> RunChange | _HubClosed | None:
    try:
        update = task.result()
    except StopAsyncIteration:
        return _HUB_CLOSED
    updated_at = await _visible_updated_at(repository, update.run_id)
    if updated_at is None:
        return None
    return RunChange(update.run_id, update.status, updated_at)


async def _live_step(
    task: asyncio.Task[RunStep], repository: RunAccessRepository
) -> RunStep | _HubClosed | None:
    try:
        step = task.result()
    except StopAsyncIteration:
        return _HUB_CLOSED
    visible_at = await _visible_updated_at(repository, step.run_id)
    if visible_at is None:
        return None
    return step


async def iter_visible_run_events(
    event_hub: RunUpdateStream,
    step_hub: RunStepStream,
    repository: RunAccessRepository,
    *,
    since: datetime | None,
    expires_at: int | None,
    now: Callable[[], float],
    keepalive_interval: float,
    replay_overlap: timedelta,
    replay_limit: int,
) -> AsyncIterator[RunChange | RunStep | StreamKeepalive]:
    """Yield visible status changes and steps until the access token expires.

    Both hubs are subscribed before status replay. Replay yields only status.
    A live status or step whose run is not visible is dropped. Steps stay on the
    step hub's FIFO; this loop never writes them into the status hub.
    """
    async with event_hub.subscribe() as updates, step_hub.subscribe() as steps:

        async def _next_update() -> RunUpdated:
            return await anext(updates)

        async def _next_step() -> RunStep:
            return await anext(steps)

        status_next: asyncio.Task[RunUpdated] | None = None
        step_next: asyncio.Task[RunStep] | None = None
        status_open = True
        step_open = True
        try:
            async for change in _replay_status(
                repository,
                since=since,
                expires_at=expires_at,
                now=now,
                replay_overlap=replay_overlap,
                replay_limit=replay_limit,
            ):
                yield change
            while status_open or step_open:
                if expires_at is not None and now() >= expires_at:
                    break
                timeout = keepalive_interval
                if expires_at is not None:
                    remaining = max(0.0, expires_at - now())
                    if remaining <= 0:
                        break
                    timeout = min(timeout, remaining)
                if status_open and status_next is None:
                    status_next = asyncio.create_task(_next_update())
                if step_open and step_next is None:
                    step_next = asyncio.create_task(_next_step())
                waiting: list[asyncio.Task[RunUpdated] | asyncio.Task[RunStep]] = [
                    task for task in (status_next, step_next) if task is not None
                ]
                if not waiting:
                    break
                done, _ = await asyncio.wait(
                    waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    if expires_at is not None and now() >= expires_at:
                        break
                    yield StreamKeepalive()
                    continue
                if status_next is not None and status_next in done:
                    finished = status_next
                    status_next = None
                    status_item = await _live_status(finished, repository)
                    if isinstance(status_item, _HubClosed):
                        status_open = False
                    elif status_item is not None:
                        yield status_item
                if step_next is not None and step_next in done:
                    finished_step = step_next
                    step_next = None
                    step_item = await _live_step(finished_step, repository)
                    if isinstance(step_item, _HubClosed):
                        step_open = False
                    elif step_item is not None:
                        yield step_item
                # A finite status iterator ends the response. Drain steps already
                # queued; do not wait for a step that has not been published.
                if not status_open and step_open:
                    if step_next is None:
                        step_next = asyncio.create_task(_next_step())
                    if not step_next.done():
                        ready, _ = await asyncio.wait({step_next}, timeout=0)
                        if not ready:
                            break
        finally:
            for task in (status_next, step_next):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await task
