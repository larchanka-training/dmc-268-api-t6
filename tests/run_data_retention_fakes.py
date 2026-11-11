"""Shared clock, store, and UoW fakes for run data retention tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

from app.modules.reviews.application.run_data_retention import PurgeExpiredRunData
from tests.leader_task_fakes import Clock as RetentionClock

DEFAULT_RETENTION_NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


class RetentionStore:
    """Answers each batch from a script; 0 once it runs out. `cost` is seconds per batch."""

    def __init__(
        self,
        clock: RetentionClock,
        *,
        runs: Sequence[int | BaseException] = (),
        blobs: Sequence[int | BaseException] = (),
        cost: float = 0.0,
    ) -> None:
        self._clock = clock
        self._runs = iter(runs)
        self._blobs = iter(blobs)
        self._cost = cost
        self.calls: list[str] = []
        self.commits = 0
        self.seen: list[tuple[Any, ...]] = []

    def _answer(self, answer: int | BaseException) -> int:
        self._clock.now += self._cost
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def purge_expired_runs(self, before: datetime, now: datetime, limit: int) -> int:
        self.calls.append("runs")
        self.seen.append((before, now, limit))
        return self._answer(next(self._runs, 0))

    async def clear_expired_blobs(self, now: datetime, limit: int) -> int:
        self.calls.append("blobs")
        self.seen.append((now, limit))
        return self._answer(next(self._blobs, 0))


class RetentionUow:
    def __init__(self, store: RetentionStore) -> None:
        self.retention = store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.retention.commits += 1

    async def rollback(self) -> None:
        return None


def make_retention_task(
    store: RetentionStore,
    clock: RetentionClock,
    *,
    now: Callable[[], datetime] = lambda: DEFAULT_RETENTION_NOW,
) -> PurgeExpiredRunData:
    return PurgeExpiredRunData(
        uow=lambda: RetentionUow(store),
        retention=timedelta(days=90),
        now=now,
        clock=clock,
    )
