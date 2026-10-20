"""The graph index's leader tasks against fakes of their ports (#143, ADR 0001 §4-§6).

Gates on a fake monotonic clock, isolation of a failing or hanging task, the shared tick
deadline, garbage collection running on consecutive ticks until drained, its round-robin
cursor, and one short transaction per store call.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Self, cast
from uuid import UUID

import pytest

import app.modules.code_index.application.leader_tasks as leader_tasks
from app.modules.code_index.application.index_store import CodeIndexStore
from app.modules.code_index.application.leader_tasks import (
    CodeIndexLeaderTasks,
    CollectIndexGarbage,
    PurgeUnusedIndexes,
    ScheduledTask,
)
from app.modules.code_index.application.maintenance import CodeIndexMaintenance

_TTL = timedelta(days=30)
R1, R2, R3, R4 = (UUID(int=number) for number in range(1, 5))


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Tracker:
    """The interleaving of transactions and store calls."""

    open: int = 0
    transactions: int = 0
    log: list[str] = field(default_factory=list)

    def call(self, name: str) -> None:
        assert self.open == 1, f"store call {name} outside one transaction"
        self.log.append(name)


def _short(repository_id: UUID | None) -> str:
    return "-" if repository_id is None else f"r{repository_id.int}"


@dataclass
class Index:
    """The store and the enumeration; each store call costs `cost` seconds of the clock."""

    tracker: Tracker
    clock: Clock
    cost: float = 0.0
    # Unused repositories and whether `purge_unused` purges each (False: used meanwhile).
    unused_ids: dict[UUID, bool] = field(default_factory=dict)
    # Rows each `collect_garbage` portion of a repository deletes, in order.
    garbage: dict[UUID, list[int]] = field(default_factory=dict)
    # `collect_garbage` never returns, like a statement stuck on a lock.
    hang: bool = False

    def _call(self, name: str) -> None:
        self.tracker.call(name)
        self.clock.now += self.cost

    async def unused(self, unused_for: timedelta, after: UUID | None, limit: int) -> list[UUID]:
        assert unused_for == _TTL
        self.tracker.call(f"unused {_short(after)} {limit}")
        return sorted(id_ for id_ in self.unused_ids if after is None or id_ > after)[:limit]

    async def purge_unused(self, repository_id: UUID, unused_for: timedelta) -> bool:
        assert unused_for == _TTL
        self._call(f"purge_unused {_short(repository_id)}")
        return self.unused_ids.pop(repository_id)

    async def with_garbage(self, after: UUID | None, limit: int) -> list[UUID]:
        self.tracker.call(f"with_garbage {_short(after)} {limit}")
        return sorted(id_ for id_ in self.garbage if after is None or id_ > after)[:limit]

    async def revoke_expired_lease(self, repository_id: UUID) -> bool:
        self.tracker.call(f"revoke {_short(repository_id)}")
        return False

    async def collect_garbage(self, repository_id: UUID, limit: int) -> int:
        self._call(f"collect {_short(repository_id)} {limit}")
        if self.hang:
            await asyncio.Event().wait()
        portions = self.garbage[repository_id]
        deleted = portions.pop(0)
        if not portions:
            del self.garbage[repository_id]
        return deleted


@dataclass
class Uow:
    tracker: Tracker
    fake: Index

    async def __aenter__(self) -> Self:
        self.tracker.open += 1
        self.tracker.transactions += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.tracker.open -= 1

    async def commit(self) -> None:
        self.tracker.call("commit")

    async def rollback(self) -> None:
        self.tracker.call("rollback")

    # The fake implements only the methods the leader tasks call.
    @property
    def index(self) -> CodeIndexStore:
        return cast(CodeIndexStore, self.fake)

    @property
    def maintenance(self) -> CodeIndexMaintenance:
        return cast(CodeIndexMaintenance, self.fake)


def _gc(index: Index, clock: Clock, batch_rows: int = 3) -> CollectIndexGarbage:
    return CollectIndexGarbage(
        uow=lambda: Uow(index.tracker, index), batch_rows=batch_rows, clock=clock
    )


def _ttl(index: Index, clock: Clock) -> PurgeUnusedIndexes:
    return PurgeUnusedIndexes(uow=lambda: Uow(index.tracker, index), unused_for=_TTL, clock=clock)


# --- CodeIndexLeaderTasks ----------------------------------------------------------


type _Runs = list[tuple[str, float, float]]


@dataclass
class Recorder:
    """A task that records when it ran with which budget and spends `cost` seconds."""

    name: str
    clock: Clock
    runs: _Runs
    cost: float = 0.0
    error: BaseException | None = None
    remains: bool = False

    async def run(self, budget_seconds: float) -> bool:
        self.runs.append((self.name, self.clock.now, budget_seconds))
        self.clock.now += self.cost
        if self.error is not None:
            raise self.error
        return self.remains


def test_a_task_runs_on_the_first_tick_and_then_once_its_interval_has_passed() -> None:
    clock = Clock()
    runs: _Runs = []
    tasks = CodeIndexLeaderTasks(
        [
            ScheduledTask("ttl", Recorder("ttl", clock, runs), 3_600),
            ScheduledTask("gc", Recorder("gc", clock, runs), 300),
        ],
        clock=clock,
    )

    for now in (0, 299, 300, 3_599, 3_600):
        clock.now = now
        asyncio.run(tasks.tick())

    assert runs == [
        ("ttl", 0, 10.0),
        ("gc", 0, 10.0),
        ("gc", 300, 10.0),
        ("gc", 3_599, 10.0),
        ("ttl", 3_600, 10.0),
    ]


def test_a_failing_task_is_logged_and_neither_stops_the_others_nor_the_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    runs: _Runs = []
    failing = Recorder("ttl", clock, runs, error=RuntimeError("database is gone"))
    tasks = CodeIndexLeaderTasks(
        [ScheduledTask("ttl", failing, 60), ScheduledTask("gc", Recorder("gc", clock, runs), 60)],
        clock=clock,
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(tasks.tick())
        clock.now = 30
        asyncio.run(tasks.tick())
        clock.now = 60
        asyncio.run(tasks.tick())

    # The failing task's gate moved on too: it runs once per interval, not every tick.
    assert runs == [("ttl", 0, 10.0), ("gc", 0, 10.0), ("ttl", 60, 10.0), ("gc", 60, 10.0)]
    assert [record.getMessage() for record in caplog.records] == [
        "Code index leader task ttl failed",
        "Code index leader task ttl failed",
    ]


def test_the_tasks_of_a_tick_share_one_deadline() -> None:
    clock = Clock()
    runs: _Runs = []
    tasks = CodeIndexLeaderTasks(
        [
            ScheduledTask("ttl", Recorder("ttl", clock, runs, cost=4), 3_600),
            ScheduledTask("gc", Recorder("gc", clock, runs, cost=7), 300),
            ScheduledTask("third", Recorder("third", clock, runs), 300),
        ],
        clock=clock,
    )

    asyncio.run(tasks.tick())
    # The deadline passed during gc: the third task keeps its gate open for the next tick.
    clock.now = 30
    asyncio.run(tasks.tick())

    assert runs == [("ttl", 0, 10.0), ("gc", 4, 6.0), ("third", 30, 10.0)]


def test_a_task_with_work_left_runs_again_on_the_next_tick() -> None:
    clock = Clock()
    runs: _Runs = []
    busy = Recorder("gc", clock, runs, remains=True)
    tasks = CodeIndexLeaderTasks([ScheduledTask("gc", busy, 300)], clock=clock)

    for now in (0, 30, 60):
        clock.now = now
        asyncio.run(tasks.tick())
    busy.remains = False
    for now in (90, 120, 389, 390):
        clock.now = now
        asyncio.run(tasks.tick())

    # The gate moves on only after the run at 90 drains: by the interval from its start.
    assert [now for _, now, _ in runs] == [0, 30, 60, 90, 390]


def test_a_hanging_task_times_out_rolls_back_its_unit_and_the_next_task_runs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Real seconds: the timeout runs on the event loop's clock, not on the fake one.
    monkeypatch.setattr(leader_tasks, "TICK_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.05)
    clock = Clock()
    runs: _Runs = []
    index = Index(Tracker(), clock, garbage={R1: [3]}, hang=True)
    tasks = CodeIndexLeaderTasks(
        [
            ScheduledTask("gc", _gc(index, clock), 300),
            ScheduledTask("third", Recorder("third", clock, runs), 300),
        ],
        clock=clock,
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(tasks.tick())
        clock.now = 30
        asyncio.run(tasks.tick())

    # The batch never committed and its transaction closed; the timed-out task's gate
    # moved on like a failed one's.
    assert index.tracker.log == ["with_garbage - 100", "revoke r1", "commit", "collect r1 3"]
    assert index.tracker.open == 0
    assert runs == [("third", 0, 0.01)]
    [record] = caplog.records
    assert record.getMessage() == "Code index leader task gc failed"
    assert record.exc_info is not None and record.exc_info[0] is TimeoutError


def test_a_cancellation_in_a_task_propagates_out_of_the_tick() -> None:
    clock = Clock()
    runs: _Runs = []
    tasks = CodeIndexLeaderTasks(
        [
            ScheduledTask("ttl", Recorder("ttl", clock, runs, error=asyncio.CancelledError()), 60),
            ScheduledTask("gc", Recorder("gc", clock, runs), 60),
        ],
        clock=clock,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tasks.tick())

    # A shutdown or a lost leader lock: no later task starts.
    assert runs == [("ttl", 0, 10.0)]


# --- PurgeUnusedIndexes -------------------------------------------------------------


def test_the_ttl_purge_pages_the_candidates_one_transaction_per_purge(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(leader_tasks, "CANDIDATE_PAGE", 2)
    clock = Clock()
    index = Index(Tracker(), clock, unused_ids={R3: True, R1: True, R2: False})
    tracker = index.tracker

    with caplog.at_level(logging.INFO):
        remains = asyncio.run(_ttl(index, clock).run(10.0))

    assert tracker.log == [
        "unused - 2",
        "purge_unused r1",
        "commit",
        "purge_unused r2",
        "commit",
        "unused r2 2",
        "purge_unused r3",
        "commit",
    ]
    assert tracker.transactions == 5
    assert remains is False
    assert "Purged 2 code indexes unused for 30 days, 0:00:00" in caplog.text


def test_the_ttl_purge_starts_no_purge_after_its_deadline() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, cost=4, unused_ids=dict.fromkeys((R1, R2, R3, R4), True))
    tracker = index.tracker

    remains = asyncio.run(_ttl(index, clock).run(10.0))

    assert tracker.log == [
        "unused - 100",
        "purge_unused r1",
        "commit",
        "purge_unused r2",
        "commit",
        "purge_unused r3",
        "commit",
    ]
    assert clock.now == 12
    # r4 is left: the gate stays open.
    assert remains is True


# --- CollectIndexGarbage ------------------------------------------------------------


def test_gc_revokes_then_collects_until_a_short_batch_and_moves_on() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, garbage={R2: [0], R1: [3, 3, 1]})
    tracker = index.tracker

    remains = asyncio.run(_gc(index, clock).run(10.0))

    assert tracker.log == [
        "with_garbage - 100",
        "revoke r1",
        "commit",
        "collect r1 3",
        "commit",
        "collect r1 3",
        "commit",
        "collect r1 3",
        "commit",
        "revoke r2",
        "commit",
        "collect r2 3",
        "commit",
    ]
    assert tracker.transactions == 7
    assert index.garbage == {}
    assert remains is False


def test_gc_stops_at_its_deadline_and_the_next_run_resumes_after_the_last_drained_repository() -> (
    None
):
    clock = Clock()
    index = Index(Tracker(), clock, cost=4, garbage={R1: [1], R2: [3, 3, 1], R3: [0]})
    tracker = index.tracker
    gc = _gc(index, clock)

    first_remains = asyncio.run(gc.run(10.0))
    first_run, tracker.log = tracker.log, []
    second_remains = asyncio.run(gc.run(10.0))

    # Cut off in r2 at 12 s: the cursor stays after r1, so r2 comes first next time.
    assert first_run == [
        "with_garbage - 100",
        "revoke r1",
        "commit",
        "collect r1 3",
        "commit",
        "revoke r2",
        "commit",
        "collect r2 3",
        "commit",
        "collect r2 3",
        "commit",
    ]
    # From r1 to the last repository, then once more from the first one.
    assert tracker.log == [
        "with_garbage r1 100",
        "revoke r2",
        "commit",
        "collect r2 3",
        "commit",
        "revoke r3",
        "commit",
        "collect r3 3",
        "commit",
        "with_garbage - 100",
    ]
    assert index.garbage == {}
    assert (first_remains, second_remains) == (True, False)


def test_gc_wraps_around_to_the_repositories_before_its_cursor() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, cost=6, garbage={R2: [1], R3: [3, 1]})
    tracker = index.tracker
    gc = _gc(index, clock)
    # Cut off in r3 at 12 s, after r2.
    assert asyncio.run(gc.run(10.0)) is True
    tracker.log = []
    index.garbage[R1] = [2]

    remains = asyncio.run(gc.run(10.0))

    # After r2 the run takes r3, then wraps to r1.
    assert tracker.log == [
        "with_garbage r2 100",
        "revoke r3",
        "commit",
        "collect r3 3",
        "commit",
        "with_garbage - 100",
        "revoke r1",
        "commit",
        "collect r1 3",
        "commit",
    ]
    assert remains is False


def test_gc_drains_over_consecutive_leader_ticks_and_then_waits_its_interval() -> None:
    clock = Clock()
    # 24 rows in batches of three at 4 s each: three batches fit one tick's 10 s.
    index = Index(Tracker(), clock, cost=4, garbage={R1: [3, 3, 3, 3, 3, 3, 2], R2: [3, 1]})
    tracker = index.tracker
    tasks = CodeIndexLeaderTasks([ScheduledTask("gc", _gc(index, clock), 300)], clock=clock)
    ran: list[int] = []
    left: list[int] = []

    # The worker leader ticks every 30 s.
    for now in range(0, 391, 30):
        clock.now = now
        calls = len(tracker.log)
        asyncio.run(tasks.tick())
        if len(tracker.log) > calls:
            ran.append(now)
        left.append(sum(sum(portions) for portions in index.garbage.values()))

    # Drained within three ticks; the gate then moves on by 300 s from the tick at 60.
    assert left[:4] == [15, 6, 0, 0]
    assert ran == [0, 30, 60, 360]
