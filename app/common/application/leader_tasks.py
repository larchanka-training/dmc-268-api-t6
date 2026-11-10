"""Shared deadline and interval gate scheduler for worker leader background tasks (#143, #556)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

_LOGGER = logging.getLogger(__name__)

# The deadline all scheduled tasks of one leader tick share; not an env knob.
TICK_BUDGET_SECONDS = 10.0
# A task still running this long after its budget is cancelled, so a unit stuck on a lock
# or a connection rolls back instead of holding up the leader loop.
TASK_TIMEOUT_MARGIN_SECONDS = 5.0


class LeaderTask(Protocol):
    async def run(self, budget_seconds: float) -> bool:
        """Work for about `budget_seconds`: start no new unit once they are spent.

        Returns whether work remains, that is whether the deadline cut the run off.
        """
        ...


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    task: LeaderTask
    interval_seconds: float
    # After the tick's timeout the gate stays open and the task runs again on the next
    # tick. Only for a task that cannot time out on the same unit twice (for example,
    # head reconcile moves its cursor past a candidate before the read; SD §11).
    continue_after_timeout: bool = False


class LeaderTasks:
    """Runs each task once its interval has passed, all within one deadline per tick.

    - A gate opens on the first tick, so a leader that restarts more often than an
      interval still runs every task. While a run reports that work remains, the gate
      stays open and the task runs again on the next tick. The gate moves on by the
      interval from the start of the run that drained the work, or that raised, so a
      failing task runs once per interval, not on every tick. A run the timeout cut off
      counts as raised, except for a task with `continue_after_timeout`, whose gate stays
      open for the next tick.
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
            timeout = asyncio.timeout(budget + TASK_TIMEOUT_MARGIN_SECONDS)
            # CONTRACT: `Exception` only, so a cancellation still stops the leader. A
            # timeout cancels the unit in flight, whose transaction rolls back.
            try:
                async with timeout:
                    remains = await scheduled.task.run(budget)
            except Exception:
                _LOGGER.exception("Leader task %s failed", scheduled.name)
                # Only this tick's timeout, not a `TimeoutError` the task raised itself.
                remains = scheduled.continue_after_timeout and timeout.expired()
            if not remains:
                self._due[number] = now + scheduled.interval_seconds
