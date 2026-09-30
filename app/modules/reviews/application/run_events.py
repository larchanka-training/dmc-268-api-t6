"""In-process delivery of durable review-run lifecycle updates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class RunUpdated:
    """A lifecycle state that has already been durably persisted."""

    run_id: UUID
    status: str


class RunUpdatePublisher(Protocol):
    async def publish(self, event: RunUpdated) -> None: ...


class RunUpdateStream(RunUpdatePublisher, Protocol):
    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[RunUpdated]]: ...


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
