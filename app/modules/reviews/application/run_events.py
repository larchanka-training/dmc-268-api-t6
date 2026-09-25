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


class InMemoryRunUpdateHub:
    """Fan out events to live clients, coalescing a slow client's stale update."""

    def __init__(self, queue_size: int = 1) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[RunUpdated]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: RunUpdated) -> None:
        for subscriber in tuple(self._subscribers):
            if subscriber.full():
                subscriber.get_nowait()
            subscriber.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[RunUpdated]]:
        queue: asyncio.Queue[RunUpdated] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)

        async def events() -> AsyncIterator[RunUpdated]:
            while True:
                yield await queue.get()

        try:
            yield events()
        finally:
            self._subscribers.discard(queue)
