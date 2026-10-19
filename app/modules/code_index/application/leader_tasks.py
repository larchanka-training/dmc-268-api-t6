"""The graph index's leader tasks (#143, ADR 0001 §4-§6).

The worker leader runs them from `leader_tick`, after the sweep and the outbox replay:

- `PurgeUnusedIndexes` purges a live index unused for the TTL: any index without a purge
  reason except a `failed` one (ADR §4 lifetime row, §7);
- `CollectIndexGarbage` revokes expired leases and deletes invisible rows in batches. It
  runs every interval and keeps running on consecutive leader ticks until drained; the
  target is physical deletion of a purged index's rows within the hour (ADR §4, §6);
- `CodeIndexLeaderTasks` gives each task a gate on a monotonic clock and all of them
  one deadline per tick, so tasks whose gates open together never hold up the sweep and
  the replay of the next tick for long.

Every store call is a short transaction of its own; no network call runs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from app.modules.code_index.application.maintenance import CodeIndexMaintenanceUnitOfWork

_LOGGER = logging.getLogger(__name__)

# The deadline all index tasks of one leader tick share; not an env knob.
TICK_BUDGET_SECONDS = 10.0
# A task still running this long after its budget is cancelled, so a unit stuck on a lock
# or a connection rolls back instead of holding up the leader loop.
TASK_TIMEOUT_MARGIN_SECONDS = 5.0
# Candidate repositories one enumeration call lists.
CANDIDATE_PAGE = 100


class LeaderTask(Protocol):
    async def run(self, budget_seconds: float) -> bool:
        """Work for about `budget_seconds`: start no new unit once they are spent.

        Returns whether work remains, that is whether the deadline cut the run off.
        """
        ...


class PurgeUnusedIndexes:
    """The TTL purge: pages `unused` and purges each candidate in a transaction of its own.

    `purge_unused` re-checks the candidate under its row lock, so a use in between keeps
    the index. Every run starts from the first candidate: a purged index is no candidate
    any more.
    """

    def __init__(
        self,
        *,
        uow: Callable[[], CodeIndexMaintenanceUnitOfWork],
        unused_for: timedelta,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow
        self._unused_for = unused_for
        self._clock = clock

    @property
    def unused_for(self) -> timedelta:
        return self._unused_for

    async def run(self, budget_seconds: float) -> bool:
        deadline = self._clock() + budget_seconds
        purged, after = 0, None
        try:
            while self._clock() < deadline:
                async with self._uow() as uow:
                    candidates = await uow.maintenance.unused(
                        self._unused_for, after, CANDIDATE_PAGE
                    )
                for repository_id in candidates:
                    if self._clock() >= deadline:
                        return True
                    async with self._uow() as uow:
                        if await uow.index.purge_unused(repository_id, self._unused_for):
                            purged += 1
                        await uow.commit()
                if len(candidates) < CANDIDATE_PAGE:
                    return False
                after = candidates[-1]
            return True
        finally:
            if purged:
                _LOGGER.info("Purged %d code indexes unused for %s", purged, self._unused_for)


class CollectIndexGarbage:
    """Garbage collection, round-robin over the repositories `with_garbage` lists.

    Per repository: revoke an expired lease, then delete `batch_rows` rows per
    transaction until a batch deletes fewer. The in-memory cursor moves past a
    repository only once it is drained, so the next run starts with the one the
    deadline cut off; past the last repository a run wraps to the first one once. A new
    leader starts from the first repository.

    Work remains while the deadline cuts a run off. A run that drains every repository
    of its pass reports none, even if the listing still names one: a probe that disagrees
    with `collect_garbage` cannot keep the gate open.
    """

    def __init__(
        self,
        *,
        uow: Callable[[], CodeIndexMaintenanceUnitOfWork],
        batch_rows: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow
        self._batch_rows = batch_rows
        self._clock = clock
        self._after: UUID | None = None

    @property
    def batch_rows(self) -> int:
        return self._batch_rows

    async def run(self, budget_seconds: float) -> bool:
        deadline = self._clock() + budget_seconds
        from_first = self._after is None
        while self._clock() < deadline:
            async with self._uow() as uow:
                candidates = await uow.maintenance.with_garbage(self._after, CANDIDATE_PAGE)
            for repository_id in candidates:
                if self._clock() >= deadline or not await self._drain(repository_id, deadline):
                    return True
                self._after = repository_id
            if len(candidates) < CANDIDATE_PAGE:
                self._after = None
                if from_first:
                    return False
                from_first = True
        return True

    async def _drain(self, repository_id: UUID, deadline: float) -> bool:
        """Whether the repository was drained before the deadline."""
        async with self._uow() as uow:
            await uow.index.revoke_expired_lease(repository_id)
            await uow.commit()
        while self._clock() < deadline:
            async with self._uow() as uow:
                deleted = await uow.index.collect_garbage(repository_id, self._batch_rows)
                await uow.commit()
            if deleted < self._batch_rows:
                return True
        return False


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    task: LeaderTask
    interval_seconds: float


class CodeIndexLeaderTasks:
    """Runs each task once its interval has passed, all within one deadline per tick.

    - A gate opens on the first tick, so a leader that restarts more often than an
      interval still runs every task. While a run reports that work remains, the gate
      stays open and the task runs again on the next tick. The gate moves on by the
      interval from the start of the run that drained the work, or that raised, so a
      failing task runs once per interval, not on every tick.
    - Tasks run in order and share `TICK_BUDGET_SECONDS`; each gets the time left and is
      cancelled `TASK_TIMEOUT_MARGIN_SECONDS` after it. A due task reached after the
      deadline keeps its gate open for the next tick.
    - A task's `Exception`, the timeout's `TimeoutError` included, is logged; `tick()`
      never raises one. A cancellation propagates.
    """

    def __init__(
        self, tasks: Sequence[ScheduledTask], *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.tasks = tuple(tasks)
        self._clock = clock
        self._due = [-math.inf] * len(self.tasks)

    async def tick(self) -> None:
        deadline = self._clock() + TICK_BUDGET_SECONDS
        for number, scheduled in enumerate(self.tasks):
            now = self._clock()
            if now < self._due[number]:
                continue
            if now >= deadline:
                return
            budget = deadline - now
            # CONTRACT: `Exception` only, so a cancellation still stops the leader. A
            # timeout cancels the unit in flight, whose transaction rolls back.
            try:
                async with asyncio.timeout(budget + TASK_TIMEOUT_MARGIN_SECONDS):
                    remains = await scheduled.task.run(budget)
            except Exception:
                _LOGGER.exception("Code index leader task %s failed", scheduled.name)
                remains = False
            if not remains:
                self._due[number] = now + scheduled.interval_seconds
