"""Unified worker leader task scheduler and budget sharing tests (#556)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from typing import Any, Never, Self, cast

import pytest

import app.common.application.leader_tasks as common_leader_tasks
import app.modules.code_index.application.leader_tasks as index_leader_module
import app.modules.reviews.application.run_data_retention as retention_module
import app.worker as worker_module
from app.common.application.leader_tasks import (
    TASK_TIMEOUT_MARGIN_SECONDS,
    TICK_BUDGET_SECONDS,
    LeaderTasks,
    ScheduledTask,
)
from app.modules.reviews.application.run_data_retention import INTERVAL_SECONDS, PurgeExpiredRunData
from app.worker import WorkerProcess, WorkerSettings, run_worker

_NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


class _Clock:
    def __init__(self, initial: float = 0.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now


class _StubLeader:
    async def execute(self, *, limit: int = 100) -> int:
        return 0

    async def replay_pending_publications(self, *, limit: int = 100) -> int:
        return 0


class _IndexWorkTask:
    def __init__(
        self,
        clock: _Clock,
        *,
        cost: float,
        remains: bool = True,
        hang: bool = False,
        hang_advance: float = 0.0,
    ) -> None:
        self._clock = clock
        self._cost = cost
        self._remains = remains
        self._hang = hang
        self._hang_advance = hang_advance
        self.budgets: list[float] = []

    async def run(self, budget_seconds: float) -> bool:
        self.budgets.append(budget_seconds)
        if self._hang:
            self._clock.now += self._hang_advance
            await asyncio.Event().wait()
        self._clock.now += self._cost
        return self._remains


class _RetentionStore:
    def __init__(self, clock: _Clock, *, runs: Sequence[int] = (), cost: float = 0.0) -> None:
        self._clock = clock
        self._runs = iter(runs)
        self._cost = cost
        self.calls: list[str] = []

    async def purge_expired_runs(self, before: datetime, now: datetime, limit: int) -> int:
        self.calls.append("runs")
        self._clock.now += self._cost
        return next(self._runs, 0)

    async def clear_expired_blobs(self, now: datetime, limit: int) -> int:
        self.calls.append("blobs")
        self._clock.now += self._cost
        return 0


class _RetentionUow:
    def __init__(self, store: _RetentionStore) -> None:
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
        return None

    async def rollback(self) -> None:
        return None


def _retention(store: _RetentionStore, clock: _Clock) -> PurgeExpiredRunData:
    return PurgeExpiredRunData(
        uow=lambda: _RetentionUow(store),
        retention=timedelta(days=90),
        now=lambda: _NOW,
        clock=clock,
    )


def _worker_process(tasks: list[ScheduledTask], clock: Callable[[], float]) -> WorkerProcess:
    stub = _StubLeader()
    return WorkerProcess(
        handle_run=cast(Any, None),
        handle_delivery=cast(Any, None),
        publish_review=cast(Any, None),
        enqueue=cast(Any, stub),
        sweep=cast(Any, stub),
        leader_tasks=LeaderTasks(tasks, clock=clock),
    )


def test_modules_do_not_duplicate_leader_scheduler_constants_or_gates() -> None:
    assert TICK_BUDGET_SECONDS == 10.0
    assert TASK_TIMEOUT_MARGIN_SECONDS == 5.0
    for attr in (
        "TICK_BUDGET_SECONDS",
        "TASK_TIMEOUT_MARGIN_SECONDS",
        "ScheduledTask",
        "CodeIndexLeaderTasks",
    ):
        assert not hasattr(index_leader_module, attr)
    for attr in ("TICK_BUDGET_SECONDS", "TICK_TIMEOUT_MARGIN_SECONDS"):
        assert not hasattr(retention_module, attr)
    assert not hasattr(PurgeExpiredRunData, "tick")


def test_index_and_retention_with_remaining_work_share_one_tick_budget() -> None:
    clock = _Clock()
    index = _IndexWorkTask(clock, cost=6.0, remains=True)
    retention_store = _RetentionStore(clock, runs=(100, 100), cost=4.0)
    retention_task = _retention(retention_store, clock)
    process = _worker_process(
        [
            ScheduledTask("gc", index, 300),
            ScheduledTask("run_data_retention", retention_task, INTERVAL_SECONDS),
        ],
        clock=clock,
    )

    asyncio.run(process.leader_tick())

    # Both had remaining work and shared one 10s tick budget: index took 6s, retention took 4s.
    assert index.budgets == [10.0]
    assert retention_store.calls == ["runs"]
    assert clock.now == 10.0

    # Both gates stay open because both reported remains=True; next tick resumes both.
    clock.now = 30.0
    asyncio.run(process.leader_tick())

    assert index.budgets == [10.0, 10.0]
    assert retention_store.calls == ["runs", "runs"]
    assert clock.now == 40.0


def test_hung_index_task_spends_only_one_timeout_margin_for_the_whole_tick(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(common_leader_tasks, "TICK_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(common_leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.02)
    clock = _Clock()
    hanging_index = _IndexWorkTask(clock, cost=0.0, hang=True, hang_advance=0.03)
    retention_store = _RetentionStore(clock)
    retention_task = _retention(retention_store, clock)
    process = _worker_process(
        [
            ScheduledTask("gc", hanging_index, 300),
            ScheduledTask("run_data_retention", retention_task, INTERVAL_SECONDS),
        ],
        clock=clock,
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(process.leader_tick())

    # One timeout margin elapsed in this tick; retention did not start past the shared deadline.
    assert hanging_index.budgets == [0.01]
    assert retention_store.calls == []
    assert clock.now == 0.03
    assert "Leader task gc failed" in caplog.text

    # Retention kept its gate open and runs on the next tick.
    clock.now = 30.0
    asyncio.run(process.leader_tick())

    assert len(hanging_index.budgets) == 1
    assert retention_store.calls == ["runs", "blobs"]


def test_hung_retention_after_index_work_receives_only_remaining_budget_and_one_margin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(common_leader_tasks, "TICK_BUDGET_SECONDS", 0.06)
    monkeypatch.setattr(common_leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.02)
    clock = _Clock()
    index = _IndexWorkTask(clock, cost=0.04, remains=True)
    retention_timeouts: list[float] = []

    class _HangingRetention:
        async def run(self, budget_seconds: float) -> bool:
            retention_timeouts.append(budget_seconds)
            await asyncio.Event().wait()
            return True

    process = _worker_process(
        [
            ScheduledTask("gc", index, 300),
            ScheduledTask("run_data_retention", _HangingRetention(), INTERVAL_SECONDS),
        ],
        clock=clock,
    )

    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        with caplog.at_level(logging.ERROR):
            loop.run_until_complete(process.leader_tick())
        elapsed = loop.time() - started
    finally:
        loop.close()

    assert index.budgets == [0.06]
    assert retention_timeouts == [pytest.approx(0.02)]
    # Retention waited only (budget_left + margin) instead of a full second budget + margin.
    assert elapsed < 0.07
    assert "Leader task run_data_retention failed" in caplog.text


def test_run_worker_combines_index_and_retention_in_one_leader_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = WorkerSettings.from_environment(
        {"DATABASE_URL": "postgresql+psycopg://test", "RABBITMQ_URL": "amqp://test"}
    )
    composed: dict[str, Any] = {}

    class Composed(Exception):
        pass

    @asynccontextmanager
    async def channels(*_: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(publisher=object(), connection=object())

    @asynccontextmanager
    async def index_consumer(*_: object) -> AsyncIterator[None]:
        yield None

    def compose(**kwargs: Any) -> Never:
        composed.update(kwargs)
        raise Composed

    monkeypatch.setattr(worker_module, "amqp_channels", channels)
    monkeypatch.setattr(worker_module, "index_consumer", index_consumer)
    monkeypatch.setattr(worker_module, "compose_worker_process", compose)
    with pytest.raises(Composed):
        asyncio.run(run_worker(settings))

    leader = composed["leader_tasks"]
    assert isinstance(leader, LeaderTasks)
    assert [(task.name, task.interval_seconds) for task in leader.tasks] == [
        ("converge", 60),
        ("gc", 300),
        ("ttl", 3_600),
        ("run_data_retention", INTERVAL_SECONDS),
    ]
    assert isinstance(leader.tasks[-1].task, PurgeExpiredRunData)
