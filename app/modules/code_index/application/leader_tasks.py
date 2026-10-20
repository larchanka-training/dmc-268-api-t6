"""The graph index's leader tasks (#143, ADR 0001 §4-§6).

The worker leader runs them from `leader_tick`, after the sweep and the outbox replay:

- `PurgeUnusedIndexes` purges a live index unused for the TTL: any index without a purge
  reason except a `failed` one (ADR §4 lifetime row, §7);
- `CollectIndexGarbage` revokes expired leases and deletes invisible rows in batches. It
  runs every interval and keeps running on consecutive leader ticks until drained; the
  target is physical deletion of a purged index's rows within the hour (ADR §4, §6);
- `ConvergeIndexes` re-arms the index of a repository with RAG on whose index is missing
  or was purged by a removal or a toggle (a reinstall, a repeated `added`, a repository
  enabled before the triggers), and purges a live index whose RAG is off;
- `DispatchIndexWakeups` wakes the job of every marked state row after the trigger's
  commit and clears the marker it read: the wake-up outbox (#143);
- `CodeIndexLeaderTasks` gives each task a gate on a monotonic clock and all of them
  one deadline per tick, so tasks whose gates open together never hold up the sweep and
  the replay of the next tick for long.

Every store call is a short transaction of its own; a wake-up is published outside any
transaction.
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
from app.modules.code_index.application.ports import IndexWakeups

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


class ConvergeIndexes:
    """Brings the state rows in line with the effective RAG of their repositories.

    One page of `to_rearm`, then one of `to_disarm`, per run: a candidate a re-check
    rejects stays out of a re-listing in the same run. Per candidate one transaction:
    the re-check under a share lock of the `repositories` row, then `enable` (which
    sets the wake-up marker) or `purge` with the re-checked reason, then the commit.
    A candidate whose transaction raises is logged and skipped, so one broken row does
    not hold up the others; a cancellation propagates. Work remains while a page was
    full or the deadline cut the run off.
    """

    def __init__(
        self,
        *,
        uow: Callable[[], CodeIndexMaintenanceUnitOfWork],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow
        self._clock = clock

    async def run(self, budget_seconds: float) -> bool:
        deadline = self._clock() + budget_seconds
        rearmed = disarmed = 0
        try:
            async with self._uow() as uow:
                rearm = await uow.maintenance.to_rearm(CANDIDATE_PAGE)
            for repository_id in rearm:
                if self._clock() >= deadline:
                    return True
                # CONTRACT: `Exception` only, so a cancellation still stops the run.
                try:
                    async with self._uow() as uow:
                        due = await uow.maintenance.rearm_due(repository_id)
                        if due:
                            await uow.index.enable(repository_id)
                        await uow.commit()
                except Exception:
                    # SD §13: the internal id only, no customer data.
                    _LOGGER.exception("Failed to re-arm the code index of %s", repository_id)
                    continue
                if due:
                    rearmed += 1
            if self._clock() >= deadline:
                return True
            async with self._uow() as uow:
                disarm = await uow.maintenance.to_disarm(CANDIDATE_PAGE)
            for candidate in disarm:
                if self._clock() >= deadline:
                    return True
                # CONTRACT: `Exception` only, so a cancellation still stops the run.
                try:
                    async with self._uow() as uow:
                        reason = await uow.maintenance.disarm_reason(candidate.repository_id)
                        if reason is not None:
                            await uow.index.purge(candidate.repository_id, reason)
                        await uow.commit()
                except Exception:
                    _LOGGER.exception(
                        "Failed to disarm the code index of %s", candidate.repository_id
                    )
                    continue
                if reason is not None:
                    disarmed += 1
            return len(rearm) == CANDIDATE_PAGE or len(disarm) == CANDIDATE_PAGE
        finally:
            if rearmed or disarmed:
                _LOGGER.info("Re-armed %d and disarmed %d code indexes", rearmed, disarmed)


class DispatchIndexWakeups:
    """The wake-up outbox: wakes the job of each marked row, then clears its marker.

    One page of `wake_requests` per run, oldest marker first. `wake` runs outside any
    transaction; the compare-and-set clear that follows misses when a trigger set a
    newer marker meanwhile, which the next run dispatches. A failed `wake` raises: the
    run stops and every marker not yet cleared stays. A duplicate wake-up is harmless:
    the job's claim refuses it (ADR §6.2).
    """

    def __init__(
        self,
        *,
        uow: Callable[[], CodeIndexMaintenanceUnitOfWork],
        wakeups: IndexWakeups,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow
        self._wakeups = wakeups
        self._clock = clock

    async def run(self, budget_seconds: float) -> bool:
        deadline = self._clock() + budget_seconds
        woken = 0
        try:
            async with self._uow() as uow:
                requests = await uow.maintenance.wake_requests(CANDIDATE_PAGE)
            for request in requests:
                if self._clock() >= deadline:
                    return True
                await self._wakeups.wake(request.repository_id, request.generation)
                woken += 1
                async with self._uow() as uow:
                    await uow.index.clear_wake_request(request.repository_id, request.requested_at)
                    await uow.commit()
            return len(requests) == CANDIDATE_PAGE
        finally:
            if woken:
                _LOGGER.info("Woke %d code index jobs", woken)


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
