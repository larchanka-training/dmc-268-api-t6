"""In-process delivery of durable review-run lifecycle updates."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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


class RunUpdatePublisher(Protocol):
    async def publish(self, event: RunUpdated) -> None: ...


class RunUpdateStream(RunUpdatePublisher, Protocol):
    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[RunUpdated]]: ...


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
