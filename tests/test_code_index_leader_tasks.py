"""The graph index's leader tasks against fakes of their ports (#143, ADR 0001 §4-§6).

Gates on a fake monotonic clock, isolation of a failing or hanging task, the shared tick
deadline, garbage collection running on consecutive ticks until drained, its round-robin
cursor, and one short transaction per store call. The converge pass re-arms and disarms
after a re-check, one transaction per candidate; the dispatch wakes outside any
transaction and then clears the marker it read. The reconcile reads each head outside
any transaction with one token cache per run, skips a rate-limited installation and a
repository it cannot read, and resumes its cursor batch by batch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast
from uuid import UUID

import pytest

import app.modules.code_index.application.leader_tasks as leader_tasks
from app.bootstrap.code_index_worker import CodeIndexSettings
from app.modules.code_index.application.build_index import IndexVersions
from app.modules.code_index.application.index_store import CodeIndexStore, IndexProfile
from app.modules.code_index.application.leader_tasks import (
    CodeIndexLeaderTasks,
    CollectIndexGarbage,
    ConvergeIndexes,
    DispatchIndexWakeups,
    PurgeUnusedIndexes,
    ReconcileIndexHeads,
    ScheduledTask,
)
from app.modules.code_index.application.maintenance import (
    CodeIndexMaintenance,
    DisarmCandidate,
    HeadCandidate,
    WakeRequest,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.application.vcs import (
    BranchHead,
    RepositorySnapshotSource,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.domain.enums import PurgeReason

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
    # Re-arm candidates and whether the re-check under the row lock still holds.
    rearm: dict[UUID, bool] = field(default_factory=dict)
    # Candidates whose re-check raises, like a row a statement cannot read.
    broken: set[UUID] = field(default_factory=set)
    # Disarm candidates and the reason the re-check returns (`None`: RAG is on again).
    disarm: dict[UUID, PurgeReason | None] = field(default_factory=dict)
    # Marked rows: the generation and the marker.
    marked: dict[UUID, tuple[int, datetime]] = field(default_factory=dict)
    # Reconcile candidates in `repository_id` order.
    candidates: list[HeadCandidate] = field(default_factory=list)
    # The profile of each locked row the reconcile reads; `_BUILT` when absent.
    profiles: dict[UUID, IndexProfile | None] = field(default_factory=dict)
    # The read time each `reconcile_head` received.
    read_at: dict[UUID, datetime] = field(default_factory=dict)

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

    async def to_rearm(self, limit: int) -> list[UUID]:
        self.tracker.call(f"to_rearm {limit}")
        return sorted(self.rearm)[:limit]

    async def rearm_due(self, repository_id: UUID) -> bool:
        self._call(f"rearm_due {_short(repository_id)}")
        if repository_id in self.broken:
            raise RuntimeError("the row cannot be read")
        return self.rearm.pop(repository_id)

    async def enable(self, repository_id: UUID) -> None:
        self.tracker.call(f"enable {_short(repository_id)}")

    async def to_disarm(self, limit: int) -> list[DisarmCandidate]:
        self.tracker.call(f"to_disarm {limit}")
        return [DisarmCandidate(id_, PurgeReason.DISABLED) for id_ in sorted(self.disarm)[:limit]]

    async def disarm_reason(self, repository_id: UUID) -> PurgeReason | None:
        self._call(f"disarm_reason {_short(repository_id)}")
        if repository_id in self.broken:
            raise RuntimeError("the row cannot be read")
        return self.disarm.pop(repository_id)

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        self.tracker.call(f"purge {_short(repository_id)} {reason}")

    async def wake_requests(self, limit: int) -> list[WakeRequest]:
        self.tracker.call(f"wake_requests {limit}")
        ordered = sorted(self.marked.items(), key=lambda item: item[1][1])
        return [WakeRequest(id_, generation, at) for id_, (generation, at) in ordered][:limit]

    async def heads(self, after: UUID | None, limit: int) -> list[HeadCandidate]:
        self.tracker.call(f"heads {_short(after)} {limit}")
        return [
            candidate
            for candidate in self.candidates
            if after is None or candidate.repository.repository_id > after
        ][:limit]

    async def reconcile_head(
        self,
        repository_id: UUID,
        *,
        generation: int,
        branch: str,
        commit_sha: str,
        read_at: datetime,
        versions_current: Callable[[IndexProfile], bool],
    ) -> bool:
        self.read_at[repository_id] = read_at
        profile = self.profiles.get(repository_id, _BUILT)
        current = "current" if profile is not None and versions_current(profile) else "outdated"
        self.tracker.call(
            f"reconcile {_short(repository_id)} g{generation} {branch}@{commit_sha} {current}"
        )
        return True

    async def clear_wake_request(self, repository_id: UUID, requested_at: datetime) -> bool:
        self.tracker.call(f"clear {_short(repository_id)} {requested_at:%H:%M}")
        if self.marked.get(repository_id, (0, None))[1] != requested_at:
            return False
        del self.marked[repository_id]
        return True


@dataclass
class Wakeups:
    """`IndexWakeups`: each wake costs `cost` seconds; `fail_on` makes a wake raise."""

    tracker: Tracker
    clock: Clock
    cost: float = 0.0
    fail_on: UUID | None = None

    async def wake(self, repository_id: UUID, generation: int) -> None:
        assert self.tracker.open == 0, "wake() inside a transaction"
        self.tracker.log.append(f"wake {_short(repository_id)} g{generation}")
        self.clock.now += self.cost
        if repository_id == self.fail_on:
            raise ConnectionError("the broker is unreachable")


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


def _converge(index: Index, clock: Clock) -> ConvergeIndexes:
    return ConvergeIndexes(uow=lambda: Uow(index.tracker, index), clock=clock)


def _dispatch(index: Index, clock: Clock, wakeups: Wakeups) -> DispatchIndexWakeups:
    return DispatchIndexWakeups(uow=lambda: Uow(index.tracker, index), wakeups=wakeups, clock=clock)


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


@dataclass
class HangsOnce:
    """A task whose first run hangs until the tick's timeout cancels it."""

    name: str
    clock: Clock
    runs: _Runs
    hung: bool = False

    async def run(self, budget_seconds: float) -> bool:
        self.runs.append((self.name, self.clock.now, budget_seconds))
        if not self.hung:
            self.hung = True
            await asyncio.Event().wait()
        return False


@pytest.mark.parametrize(
    ("name", "interval", "continues", "ran_at"),
    [
        pytest.param("reconcile", 600, True, [0, 30], id="reconcile-runs-on-the-next-tick"),
        pytest.param("gc", 300, False, [0, 300], id="gc-waits-its-interval"),
    ],
)
def test_after_a_timeout_only_a_task_that_continues_runs_again_on_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    interval: float,
    continues: bool,
    ran_at: list[float],
) -> None:
    # Real seconds: the timeout runs on the event loop's clock, not on the fake one.
    monkeypatch.setattr(leader_tasks, "TICK_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.05)
    clock = Clock()
    runs: _Runs = []
    tasks = CodeIndexLeaderTasks(
        [
            ScheduledTask(
                name, HangsOnce(name, clock, runs), interval, continue_after_timeout=continues
            )
        ],
        clock=clock,
    )

    with caplog.at_level(logging.ERROR):
        for now in (0, 30, 300):
            clock.now = now
            asyncio.run(tasks.tick())

    # The run that drained moves the gate on by the interval from its start.
    assert [now for _, now, _ in runs] == ran_at
    [record] = caplog.records
    assert record.getMessage() == f"Code index leader task {name} failed"
    assert record.exc_info is not None and record.exc_info[0] is TimeoutError


def test_a_timeout_error_the_task_raises_itself_moves_the_gate_of_a_task_that_continues() -> None:
    clock = Clock()
    runs: _Runs = []
    raising = Recorder("reconcile", clock, runs, error=TimeoutError("the listing timed out"))
    tasks = CodeIndexLeaderTasks(
        [ScheduledTask("reconcile", raising, 600, continue_after_timeout=True)], clock=clock
    )

    for now in (0, 30, 600):
        clock.now = now
        asyncio.run(tasks.tick())

    # Only the tick's own timeout keeps the gate open; this error is a failure.
    assert [now for _, now, _ in runs] == [0, 600]


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


# --- ConvergeIndexes -----------------------------------------------------------------


def test_converge_rearms_each_candidate_after_its_recheck_in_a_transaction_of_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    # r2 was turned off between the listing and the re-check.
    index = Index(Tracker(), clock, rearm={R3: True, R1: True, R2: False})
    tracker = index.tracker

    with caplog.at_level(logging.INFO):
        remains = asyncio.run(_converge(index, clock).run(10.0))

    assert tracker.log == [
        "to_rearm 100",
        "rearm_due r1",
        "enable r1",
        "commit",
        "rearm_due r2",
        "commit",
        "rearm_due r3",
        "enable r3",
        "commit",
        "to_disarm 100",
    ]
    assert tracker.transactions == 5
    assert remains is False
    assert "Re-armed 2 and disarmed 0 code indexes" in caplog.text


def test_converge_disarms_with_the_reason_of_the_recheck() -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        disarm={R1: PurgeReason.DISABLED, R2: None, R3: PurgeReason.REMOVED},
    )
    tracker = index.tracker

    remains = asyncio.run(_converge(index, clock).run(10.0))

    assert tracker.log == [
        "to_rearm 100",
        "to_disarm 100",
        "disarm_reason r1",
        "purge r1 disabled",
        "commit",
        "disarm_reason r2",
        "commit",
        "disarm_reason r3",
        "purge r3 removed",
        "commit",
    ]
    assert tracker.transactions == 5
    assert remains is False


def test_converge_takes_one_page_of_each_listing_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leader_tasks, "CANDIDATE_PAGE", 2)
    clock = Clock()
    index = Index(
        Tracker(), clock, rearm=dict.fromkeys((R1, R2, R3), True), disarm={R4: PurgeReason.REMOVED}
    )
    tracker = index.tracker

    # A full page may leave candidates: work remains, and the next tick lists again.
    assert asyncio.run(_converge(index, clock).run(10.0)) is True
    assert asyncio.run(_converge(index, clock).run(10.0)) is False

    assert [entry for entry in tracker.log if entry.startswith(("to_", "enable", "purge"))] == [
        "to_rearm 2",
        "enable r1",
        "enable r2",
        "to_disarm 2",
        "purge r4 removed",
        "to_rearm 2",
        "enable r3",
        "to_disarm 2",
    ]


def test_converge_starts_no_recheck_after_its_deadline() -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        cost=4,
        rearm=dict.fromkeys((R1, R2, R3), True),
        disarm={R4: PurgeReason.DISABLED},
    )
    tracker = index.tracker

    remains = asyncio.run(_converge(index, clock).run(10.0))

    assert [entry for entry in tracker.log if entry != "commit"] == [
        "to_rearm 100",
        "rearm_due r1",
        "enable r1",
        "rearm_due r2",
        "enable r2",
        "rearm_due r3",
        "enable r3",
    ]
    assert clock.now == 12
    assert remains is True


def test_converge_logs_a_failing_candidate_and_goes_on_with_the_next(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        rearm={R1: True, R2: True},
        disarm={R3: PurgeReason.DISABLED, R4: PurgeReason.REMOVED},
        broken={R1, R3},
    )
    tracker = index.tracker

    with caplog.at_level(logging.INFO):
        remains = asyncio.run(_converge(index, clock).run(10.0))

    assert tracker.log == [
        "to_rearm 100",
        "rearm_due r1",
        "rearm_due r2",
        "enable r2",
        "commit",
        "to_disarm 100",
        "disarm_reason r3",
        "disarm_reason r4",
        "purge r4 removed",
        "commit",
    ]
    assert remains is False
    assert f"Failed to re-arm the code index of {R1}" in caplog.text
    assert f"Failed to disarm the code index of {R3}" in caplog.text
    assert "Re-armed 1 and disarmed 1 code indexes" in caplog.text


# --- DispatchIndexWakeups ------------------------------------------------------------

_T1 = datetime(2026, 10, 20, 9, 1, tzinfo=UTC)
_T2 = datetime(2026, 10, 20, 9, 2, tzinfo=UTC)
_T3 = datetime(2026, 10, 20, 9, 3, tzinfo=UTC)
_T4 = datetime(2026, 10, 20, 9, 4, tzinfo=UTC)


def test_dispatch_wakes_outside_a_transaction_and_then_clears_the_marker_it_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    index = Index(Tracker(), clock, marked={R2: (3, _T2), R1: (1, _T1)})
    tracker = index.tracker

    with caplog.at_level(logging.INFO):
        remains = asyncio.run(_dispatch(index, clock, Wakeups(tracker, clock)).run(10.0))

    assert tracker.log == [
        "wake_requests 100",
        "wake r1 g1",
        "clear r1 09:01",
        "commit",
        "wake r2 g3",
        "clear r2 09:02",
        "commit",
    ]
    assert tracker.transactions == 3
    assert index.marked == {}
    assert remains is False
    assert "Woke 2 code index jobs" in caplog.text


def test_a_failed_wake_stops_the_dispatch_and_every_marker_stays() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, marked={R1: (1, _T1), R2: (1, _T2)})
    tracker = index.tracker
    wakeups = Wakeups(tracker, clock, fail_on=R1)

    with pytest.raises(ConnectionError):
        asyncio.run(_dispatch(index, clock, wakeups).run(10.0))

    assert tracker.log == ["wake_requests 100", "wake r1 g1"]
    assert index.marked == {R1: (1, _T1), R2: (1, _T2)}


def test_dispatch_starts_no_wake_after_its_deadline() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, marked={R1: (1, _T1), R2: (1, _T2), R3: (1, _T3), R4: (1, _T4)})
    tracker = index.tracker

    remains = asyncio.run(_dispatch(index, clock, Wakeups(tracker, clock, cost=4)).run(10.0))

    assert [entry for entry in tracker.log if entry.startswith("wake ")] == [
        "wake r1 g1",
        "wake r2 g1",
        "wake r3 g1",
    ]
    assert index.marked == {R4: (1, _T4)}
    assert remains is True


# --- ReconcileIndexHeads -------------------------------------------------------------

_VERSIONS = IndexVersions("facts-3", "resolver-2", "parser-1", "limits-1")
_BUILT = _VERSIONS.profile("main", "d" * 64)
_OUTDATED = IndexProfile("facts-2", "resolver-2", "parser-1", "limits-1", "main", "d" * 64)
_RATE_LIMITED = VcsUnavailable("rate limited", rate_limited=True)
_LISTED = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)

# An event: the head read waits for it, like a request stuck on the network.
type _HeadOutcome = BranchHead | None | Exception | asyncio.Event


def _candidate(repository_id: UUID, installation: int, provider: str = "github") -> HeadCandidate:
    return HeadCandidate(
        IndexedRepository(repository_id, provider, installation, f"octo/r{repository_id.int}"),
        1,
        _LISTED,
    )


@dataclass
class Sources:
    """The per-run factory of snapshot sources: `runs` counts the token caches made, and
    each head read logs the run whose cache it uses. An outcome list is consumed one item
    per read; a repository without one reads `main` at `c<n>`."""

    tracker: Tracker
    clock: Clock
    outcomes: dict[UUID, list[_HeadOutcome]] = field(default_factory=dict)
    cost: float = 0.0
    runs: int = 0

    def __call__(self) -> Callable[[IndexedRepository], RepositorySnapshotSource]:
        self.runs += 1
        run = self.runs

        def source(repository: IndexedRepository) -> RepositorySnapshotSource:
            return cast(RepositorySnapshotSource, _Source(self, repository.repository_id, run))

        return source


@dataclass
class _Source:
    sources: Sources
    repository_id: UUID
    run: int

    async def head(self) -> BranchHead | None:
        tracker = self.sources.tracker
        assert tracker.open == 0, "head() inside a transaction"
        tracker.log.append(f"head {_short(self.repository_id)} run{self.run}")
        self.sources.clock.now += self.sources.cost
        pending = self.sources.outcomes.get(self.repository_id)
        outcome: _HeadOutcome = (
            pending.pop(0) if pending else BranchHead("main", f"c{self.repository_id.int}")
        )
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, asyncio.Event):
            await outcome.wait()
            return None
        return outcome


def _reconcile(
    index: Index, clock: Clock, sources: Sources, batch: int = 50
) -> ReconcileIndexHeads:
    return ReconcileIndexHeads(
        uow=lambda: Uow(index.tracker, index),
        sources=sources,
        versions=_VERSIONS,
        batch=batch,
        clock=clock,
    )


def test_reconcile_reads_each_head_outside_a_transaction_and_writes_it_in_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        candidates=[_candidate(R1, 17), _candidate(R2, 17), _candidate(R3, 18)],
        profiles={R2: None, R3: _OUTDATED},
    )
    tracker = index.tracker
    sources = Sources(tracker, clock, {R2: [BranchHead("develop", "f" * 40)]})

    with caplog.at_level(logging.INFO):
        remains = asyncio.run(_reconcile(index, clock, sources).run(10.0))

    assert tracker.log == [
        "heads - 50",
        "head r1 run1",
        "reconcile r1 g1 main@c1 current",
        "commit",
        "head r2 run1",
        f"reconcile r2 g1 develop@{'f' * 40} outdated",
        "commit",
        "head r3 run1",
        "reconcile r3 g1 main@c3 outdated",
        "commit",
    ]
    assert tracker.transactions == 4
    assert index.read_at == dict.fromkeys((R1, R2, R3), _LISTED)
    assert remains is False
    assert "Reconciled 3 code indexes with their heads; 3 need a build" in caplog.text


def test_reconcile_shares_one_token_cache_per_run() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, candidates=[_candidate(R1, 17), _candidate(R2, 18)])
    sources = Sources(index.tracker, clock)
    reconcile = _reconcile(index, clock, sources)

    asyncio.run(reconcile.run(10.0))
    asyncio.run(reconcile.run(10.0))

    assert sources.runs == 2
    assert [entry for entry in index.tracker.log if entry.startswith("head ")] == [
        "head r1 run1",
        "head r2 run1",
        "head r1 run2",
        "head r2 run2",
    ]


def test_a_rate_limit_skips_the_installation_for_the_run_and_another_installation_goes_on() -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        candidates=[_candidate(R1, 17), _candidate(R2, 17), _candidate(R3, 18)],
    )
    sources = Sources(index.tracker, clock, {R1: [_RATE_LIMITED]})
    reconcile = _reconcile(index, clock, sources)

    asyncio.run(reconcile.run(10.0))
    # Without a `retry_after` the installation is skipped for that run only.
    asyncio.run(reconcile.run(10.0))

    assert [entry for entry in index.tracker.log if entry.startswith(("head ", "reconcile"))] == [
        "head r1 run1",
        "head r3 run1",
        "reconcile r3 g1 main@c3 current",
        "head r1 run2",
        "reconcile r1 g1 main@c1 current",
        "head r2 run2",
        "reconcile r2 g1 main@c2 current",
        "head r3 run2",
        "reconcile r3 g1 main@c3 current",
    ]


def test_reconcile_rate_limit_is_scoped_by_provider_and_installation() -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        candidates=[
            _candidate(R1, 17, "bitbucket"),
            _candidate(R2, 17, "bitbucket"),
            _candidate(R3, 17, "github"),
        ],
    )
    sources = Sources(index.tracker, clock, {R1: [_RATE_LIMITED]})

    asyncio.run(_reconcile(index, clock, sources).run(10.0))

    assert [entry for entry in index.tracker.log if entry.startswith("head ")] == [
        "head r1 run1",
        "head r3 run1",
    ]


def test_a_rate_limit_with_retry_after_pauses_the_installation_until_then() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, candidates=[_candidate(R1, 17), _candidate(R2, 18)])
    limited = VcsUnavailable("rate limited", rate_limited=True, retry_after=timedelta(seconds=60))
    sources = Sources(index.tracker, clock, {R1: [limited]})
    reconcile = _reconcile(index, clock, sources)

    for now in (0, 59, 60):
        clock.now = now
        asyncio.run(reconcile.run(10.0))

    assert [entry for entry in index.tracker.log if entry.startswith("head ")] == [
        "head r1 run1",
        "head r2 run1",
        "head r2 run2",
        "head r1 run3",
        "head r2 run3",
    ]


def test_reconcile_skips_a_repository_it_cannot_read_without_a_store_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        candidates=[
            _candidate(R1, 17),
            _candidate(R2, 17),
            _candidate(R3, 17),
            _candidate(R4, 17),
        ],
    )
    sources = Sources(
        index.tracker,
        clock,
        {R1: [VcsAccessDenied("gone")], R2: [None], R3: [VcsUnavailable("bad gateway")]},
    )

    with caplog.at_level(logging.WARNING):
        remains = asyncio.run(_reconcile(index, clock, sources).run(10.0))

    assert [entry for entry in index.tracker.log if entry != "commit"] == [
        "heads - 50",
        "head r1 run1",
        "head r2 run1",
        "head r3 run1",
        "head r4 run1",
        "reconcile r4 g1 main@c4 current",
    ]
    assert remains is False
    assert "vcs_access_denied" in caplog.text and "vcs_unavailable" in caplog.text


def test_reconcile_logs_a_failing_candidate_and_goes_on_with_the_next(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    index = Index(Tracker(), clock, candidates=[_candidate(R1, 17), _candidate(R2, 17)])
    sources = Sources(index.tracker, clock, {R1: [RuntimeError("unexpected")]})

    with caplog.at_level(logging.ERROR):
        remains = asyncio.run(_reconcile(index, clock, sources).run(10.0))

    assert [entry for entry in index.tracker.log if entry != "commit"] == [
        "heads - 50",
        "head r1 run1",
        "head r2 run1",
        "reconcile r2 g1 main@c2 current",
    ]
    assert remains is False
    assert f"Failed to reconcile the code index of {R1} with its head" in caplog.text


def test_a_candidate_that_timed_out_is_read_again_only_on_the_next_pass() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, candidates=[_candidate(R1, 17), _candidate(R2, 18)])
    reconcile = _reconcile(index, clock, Sources(index.tracker, clock, {R1: [asyncio.Event()]}))

    async def runs() -> list[bool]:
        # The leader's timeout cancels the run in the head read of r1.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await reconcile.run(10.0)
        return [await reconcile.run(10.0), await reconcile.run(10.0)]

    assert asyncio.run(runs()) == [False, False]
    assert [entry for entry in index.tracker.log if entry.startswith(("heads", "head "))] == [
        "heads - 50",
        "head r1 run1",
        "heads r1 50",
        "head r2 run2",
        "heads - 50",
        "head r1 run3",
        "head r2 run3",
    ]


def test_reconcile_resumes_its_cursor_after_each_batch_and_waits_after_the_last() -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        candidates=[_candidate(R1, 17), _candidate(R2, 18), _candidate(R3, 19)],
    )
    reconcile = _reconcile(index, clock, Sources(index.tracker, clock), batch=2)

    # A full batch leaves work: the gate stays open and the next tick goes on.
    assert asyncio.run(reconcile.run(10.0)) is True
    assert asyncio.run(reconcile.run(10.0)) is False
    assert asyncio.run(reconcile.run(10.0)) is True

    assert [entry for entry in index.tracker.log if entry.startswith(("heads", "head "))] == [
        "heads - 2",
        "head r1 run1",
        "head r2 run1",
        "heads r2 2",
        "head r3 run2",
        "heads - 2",
        "head r1 run3",
        "head r2 run3",
    ]


def test_reconcile_starts_no_head_read_after_its_deadline_and_resumes_there() -> None:
    clock = Clock()
    index = Index(
        Tracker(),
        clock,
        candidates=[_candidate(R1, 17), _candidate(R2, 18), _candidate(R3, 19), _candidate(R4, 20)],
    )
    reconcile = _reconcile(index, clock, Sources(index.tracker, clock, cost=4))

    assert asyncio.run(reconcile.run(10.0)) is True
    assert asyncio.run(reconcile.run(10.0)) is False

    assert [entry for entry in index.tracker.log if entry.startswith(("heads", "head "))] == [
        "heads - 50",
        "head r1 run1",
        "head r2 run1",
        "head r3 run1",
        "heads r3 50",
        "head r4 run2",
    ]


def test_reconcile_starts_no_head_read_with_less_than_its_reserve_left() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, candidates=[_candidate(R1, 17), _candidate(R2, 18)])
    reconcile = _reconcile(index, clock, Sources(index.tracker, clock, cost=3))

    # Too short a budget lists nothing; 4 s read r1, and the 1 s left starts no read.
    assert asyncio.run(reconcile.run(1.9)) is True
    assert asyncio.run(reconcile.run(4.0)) is True
    assert asyncio.run(reconcile.run(10.0)) is False

    assert [entry for entry in index.tracker.log if entry.startswith(("heads", "head "))] == [
        "heads - 50",
        "head r1 run1",
        "heads r1 50",
        "head r2 run2",
    ]


def test_a_slow_reconcile_runs_last_on_what_is_left_and_never_starves_the_other_tasks() -> None:
    clock = Clock()
    runs: _Runs = []
    repositories = (R1, R2, R3, R4, UUID(int=5))
    index = Index(
        Tracker(),
        clock,
        candidates=[_candidate(id_, 16 + id_.int) for id_ in repositories],
    )
    tasks = CodeIndexLeaderTasks(
        [
            ScheduledTask("dispatch", Recorder("dispatch", clock, runs, cost=1), 0),
            ScheduledTask("ttl", Recorder("ttl", clock, runs, cost=1, remains=True), 3_600),
            ScheduledTask("gc", Recorder("gc", clock, runs, cost=1, remains=True), 300),
            ScheduledTask(
                "reconcile", _reconcile(index, clock, Sources(index.tracker, clock, cost=3)), 600
            ),
        ],
        clock=clock,
    )

    # Each tick the reconcile gets the 7 s the others left: two heads of 3 s, and the
    # third would start with 1 s left. Its gate stays open, and the others still run.
    asyncio.run(tasks.tick())
    asyncio.run(tasks.tick())

    assert runs == [
        ("dispatch", 0, 10),
        ("ttl", 1, 9),
        ("gc", 2, 8),
        ("dispatch", 9, 10),
        ("ttl", 10, 9),
        ("gc", 11, 8),
    ]
    assert [entry for entry in index.tracker.log if entry.startswith(("heads", "head "))] == [
        "heads - 50",
        "head r1 run1",
        "head r2 run1",
        "heads r2 50",
        "head r3 run2",
        "head r4 run2",
    ]
    assert clock.now == 18


def test_the_reconcile_gate_opens_every_reconcile_interval() -> None:
    clock = Clock()
    index = Index(Tracker(), clock, candidates=[_candidate(R1, 17)])
    interval = CodeIndexSettings().reconcile_interval_seconds
    tasks = CodeIndexLeaderTasks(
        [
            ScheduledTask(
                "reconcile", _reconcile(index, clock, Sources(index.tracker, clock)), interval
            )
        ],
        clock=clock,
    )

    for now in (0, 599, 600, 1_199, 1_200):
        clock.now = now
        asyncio.run(tasks.tick())

    assert interval == 600
    assert [entry for entry in index.tracker.log if entry.startswith("head ")] == [
        "head r1 run1",
        "head r1 run2",
        "head r1 run3",
    ]
