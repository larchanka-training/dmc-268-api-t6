"""The scheduled cleanup of stale run data (#457, SD §10).

A terminal run past the period loses the text of its diff and its context manifests. Its
verdict, findings, counters and the metadata of its diff rows stay: the run pages read them.
An expired file blob loses its text on its own expiry: many runs share one blob.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.common.application.unit_of_work import UnitOfWork

_LOGGER = logging.getLogger(__name__)

RUN_BATCH = 100
BLOB_BATCH = 1_000
INTERVAL_SECONDS = 3_600


class RunDataRetentionStore(Protocol):
    async def purge_expired_runs(self, before: datetime, now: datetime, limit: int) -> int:
        """Clear up to `limit` terminal runs finished before `before`; flushes only."""
        ...

    async def clear_expired_blobs(self, now: datetime, limit: int) -> int:
        """Drop the text of up to `limit` blobs expired by `now`; flushes only."""
        ...


class RunDataRetentionUnitOfWork(UnitOfWork, Protocol):
    @property
    def retention(self) -> RunDataRetentionStore: ...


class PurgeExpiredRunData:
    """One transaction per batch: a pass cut off by its budget keeps what it committed."""

    def __init__(
        self,
        *,
        uow: Callable[[], RunDataRetentionUnitOfWork],
        retention: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow
        self._retention = retention
        self._now = now
        self._clock = clock

    async def run(self, budget_seconds: float) -> bool:
        """Work for about `budget_seconds`; returns whether work remains."""
        deadline = self._clock() + budget_seconds
        now = self._now()
        before = now - self._retention
        purged, remains = await self._drain(
            lambda store, limit: store.purge_expired_runs(before, now, limit), RUN_BATCH, deadline
        )
        if purged:
            _LOGGER.info("Purged the data of %d runs finished before %s", purged, before)
        if remains:
            return True
        cleared, remains = await self._drain(
            lambda store, limit: store.clear_expired_blobs(now, limit), BLOB_BATCH, deadline
        )
        if cleared:
            _LOGGER.info("Cleared the text of %d expired file blobs", cleared)
        return remains

    async def _drain(
        self,
        step: Callable[[RunDataRetentionStore, int], Awaitable[int]],
        batch: int,
        deadline: float,
    ) -> tuple[int, bool]:
        """Run `step` batch by batch; returns the count and whether the deadline cut it off."""
        total = 0
        while self._clock() < deadline:
            async with self._uow() as uow:
                count = await step(uow.retention, batch)
                await uow.commit()
            total += count
            if count < batch:
                return total, False
        return total, True
