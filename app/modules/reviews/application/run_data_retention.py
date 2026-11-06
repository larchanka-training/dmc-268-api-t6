"""The scheduled cleanup of stale run data (#457, SD §10).

A terminal run past the period loses the text of its diff and its context manifests. Its
verdict, findings, counters and the metadata of its diff rows stay: the run pages read them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.common.application.unit_of_work import UnitOfWork

_LOGGER = logging.getLogger(__name__)

RUN_BATCH = 100


class RunDataRetentionStore(Protocol):
    async def purge_expired_runs(self, before: datetime, now: datetime, limit: int) -> int:
        """Clear up to `limit` terminal runs finished before `before`; flushes only."""
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
        purged = 0
        try:
            while self._clock() < deadline:
                async with self._uow() as uow:
                    count = await uow.retention.purge_expired_runs(before, now, RUN_BATCH)
                    await uow.commit()
                purged += count
                if count < RUN_BATCH:
                    return False
            return True
        finally:
            if purged:
                _LOGGER.info("Purged the data of %d runs finished before %s", purged, before)
