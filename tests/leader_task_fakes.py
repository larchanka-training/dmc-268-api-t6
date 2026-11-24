"""Shared fakes and clock for leader background tasks (#556)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from app.common.application.leader_tasks import LeaderTasks, ScheduledTask

if TYPE_CHECKING:
    from app.worker import WorkerProcess

type Runs = list[tuple[str, float, float]]


@dataclass
class Clock:
    """A callable clock whose time can be read and set manually."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Recorder:
    """A task that records when it ran with which budget and spends `cost` seconds."""

    name: str
    clock: Clock
    runs: Runs
    cost: float = 0.0
    error: BaseException | None = None
    remains: bool = False
    hang: bool = False
    hang_advance: float = 0.0
    hung_once: bool = False

    async def run(self, budget_seconds: float) -> bool:
        self.runs.append((self.name, self.clock.now, budget_seconds))
        if self.hang:
            self.clock.now += self.hang_advance
            await asyncio.Event().wait()
        if self.hung_once:
            self.hung_once = False
            await asyncio.Event().wait()
        self.clock.now += self.cost
        if self.error is not None:
            raise self.error
        return self.remains


@dataclass
class StubLeader:
    """Stub for outbox sweep and publication replay in worker leader tests."""

    calls: list[str] = field(default_factory=list)
    failing: str | None = None
    error: type[BaseException] = RuntimeError

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name == self.failing:
            raise self.error(f"{name} failed")

    async def execute(self, *, limit: int = 100) -> int:
        self._call("sweep")
        return 0

    async def replay_pending_publications(self, *, limit: int = 100) -> int:
        self._call("replay")
        return 0


def worker_process(
    leader_tasks: LeaderTasks | list[ScheduledTask],
    *,
    stub: StubLeader | None = None,
    clock: Callable[[], float] | None = None,
) -> WorkerProcess:
    from app.worker import WorkerProcess

    leader = stub or StubLeader()
    if isinstance(leader_tasks, LeaderTasks):
        tasks = leader_tasks
        if clock is not None:
            tasks._clock = clock
    elif clock is not None:
        tasks = LeaderTasks(leader_tasks, clock=clock)
    else:
        tasks = LeaderTasks(leader_tasks)
    return WorkerProcess(
        handle_run=cast(Any, None),
        handle_delivery=cast(Any, None),
        publish_review=cast(Any, None),
        enqueue=cast(Any, leader),
        sweep=cast(Any, leader),
        leader_tasks=tasks,
    )
