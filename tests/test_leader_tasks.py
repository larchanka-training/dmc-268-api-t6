"""Unified worker leader task scheduler and budget sharing tests (#556)."""

from __future__ import annotations

import ast
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

import app.common.application.leader_tasks as common_leader_tasks
import app.modules.code_index.application.leader_tasks as index_leader_module
import app.modules.reviews.application.run_data_retention as retention_module
from app.common.application.leader_tasks import (
    TASK_TIMEOUT_MARGIN_SECONDS,
    TICK_BUDGET_SECONDS,
    LeaderTasks,
    ScheduledTask,
)
from app.modules.reviews.application.run_data_retention import INTERVAL_SECONDS, PurgeExpiredRunData
from app.worker import WorkerProcess
from tests.leader_task_fakes import Clock, Recorder
from tests.leader_task_fakes import Runs as _Runs
from tests.run_data_retention_fakes import RetentionStore, make_retention_task

_APP_ROOT = Path(__file__).resolve().parents[1] / "app"


@dataclass
class _StubLeader:
    async def execute(self, *, limit: int = 100) -> int:
        return 0

    async def replay_pending_publications(self, *, limit: int = 100) -> int:
        return 0


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

    index_tree = ast.parse(
        (_APP_ROOT / "modules" / "code_index" / "application" / "leader_tasks.py").read_text(
            encoding="utf-8"
        )
    )
    retention_tree = ast.parse(
        (_APP_ROOT / "modules" / "reviews" / "application" / "run_data_retention.py").read_text(
            encoding="utf-8"
        )
    )

    index_defs = {
        node.name
        for node in ast.walk(index_tree)
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "CodeIndexLeaderTasks" not in index_defs
    assert "LeaderTasks" not in index_defs
    for attr in ("TICK_BUDGET_SECONDS", "TASK_TIMEOUT_MARGIN_SECONDS", "ScheduledTask"):
        assert not hasattr(index_leader_module, attr)

    retention_imports = [
        *(
            node.module or ""
            for node in ast.walk(retention_tree)
            if isinstance(node, ast.ImportFrom)
        ),
        *(
            alias.name
            for node in ast.walk(retention_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ),
    ]
    assert not any(mod.startswith("app.modules.code_index") for mod in retention_imports)
    retention_defs = {
        node.name
        for node in ast.walk(retention_tree)
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert not any("Scheduler" in name or "LeaderTasks" in name for name in retention_defs)
    assert not hasattr(PurgeExpiredRunData, "tick")
    for attr in ("TICK_BUDGET_SECONDS", "TICK_TIMEOUT_MARGIN_SECONDS"):
        assert not hasattr(retention_module, attr)


def test_a_task_runs_on_the_first_tick_and_then_once_its_interval_has_passed() -> None:
    clock = Clock()
    runs: _Runs = []
    tasks = LeaderTasks(
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
    tasks = LeaderTasks(
        [
            ScheduledTask("ttl", failing, 60),
            ScheduledTask("gc", Recorder("gc", clock, runs), 60),
        ],
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
        "Leader task ttl failed",
        "Leader task ttl failed",
    ]


def test_the_tasks_of_a_tick_share_one_deadline() -> None:
    clock = Clock()
    runs: _Runs = []
    tasks = LeaderTasks(
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
    tasks = LeaderTasks([ScheduledTask("gc", busy, 300)], clock=clock)

    for now in (0, 30, 60):
        clock.now = now
        asyncio.run(tasks.tick())
    busy.remains = False
    for now in (90, 120, 389, 390):
        clock.now = now
        asyncio.run(tasks.tick())

    # The gate moves on only after the run at 90 drains: by the interval from its start.
    assert [now for _, now, _ in runs] == [0, 30, 60, 90, 390]


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
    monkeypatch.setattr(common_leader_tasks, "TICK_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(common_leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.05)
    clock = Clock()
    runs: _Runs = []
    tasks = LeaderTasks(
        [
            ScheduledTask(
                name,
                Recorder(name, clock, runs, hung_once=True),
                interval,
                continue_after_timeout=continues,
            )
        ],
        clock=clock,
    )

    with caplog.at_level(logging.ERROR):
        for now in (0, 30, 300):
            clock.now = now
            asyncio.run(tasks.tick())

    assert [now for _, now, _ in runs] == ran_at
    [record] = caplog.records
    assert record.getMessage() == f"Leader task {name} failed"
    assert record.exc_info is not None and record.exc_info[0] is TimeoutError


def test_a_timeout_error_the_task_raises_itself_moves_the_gate_of_a_task_that_continues() -> None:
    clock = Clock()
    runs: _Runs = []
    raising = Recorder("reconcile", clock, runs, error=TimeoutError("the listing timed out"))
    tasks = LeaderTasks(
        [ScheduledTask("reconcile", raising, 600, continue_after_timeout=True)], clock=clock
    )

    for now in (0, 30, 600):
        clock.now = now
        asyncio.run(tasks.tick())

    assert [now for _, now, _ in runs] == [0, 600]


def test_a_cancellation_in_a_task_propagates_out_of_the_tick() -> None:
    clock = Clock()
    runs: _Runs = []
    tasks = LeaderTasks(
        [
            ScheduledTask("ttl", Recorder("ttl", clock, runs, error=asyncio.CancelledError()), 60),
            ScheduledTask("gc", Recorder("gc", clock, runs), 60),
        ],
        clock=clock,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tasks.tick())

    assert runs == [("ttl", 0, 10.0)]


def test_index_and_retention_with_remaining_work_share_one_tick_budget() -> None:
    clock = Clock()
    runs: _Runs = []
    index = Recorder("gc", clock, runs, cost=6.0, remains=True)
    retention_store = RetentionStore(clock, runs=(100, 100), cost=4.0)
    retention_task = make_retention_task(retention_store, clock)
    process = _worker_process(
        [
            ScheduledTask("gc", index, 300),
            ScheduledTask("run_data_retention", retention_task, INTERVAL_SECONDS),
        ],
        clock=clock,
    )

    asyncio.run(process.leader_tick())

    assert runs == [("gc", 0.0, 10.0)]
    assert retention_store.calls == ["runs"]
    assert clock.now == 10.0

    clock.now = 30.0
    asyncio.run(process.leader_tick())

    assert runs == [("gc", 0.0, 10.0), ("gc", 30.0, 10.0)]
    assert retention_store.calls == ["runs", "runs"]
    assert clock.now == 40.0


def test_hung_index_task_leaves_retention_gate_open_for_next_tick(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(common_leader_tasks, "TICK_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(common_leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.02)
    clock = Clock()
    runs: _Runs = []
    hanging_index = Recorder("gc", clock, runs, hang=True, hang_advance=0.03)
    retention_store = RetentionStore(clock)
    retention_task = make_retention_task(retention_store, clock)
    process = _worker_process(
        [
            ScheduledTask("gc", hanging_index, 300),
            ScheduledTask("run_data_retention", retention_task, INTERVAL_SECONDS),
        ],
        clock=clock,
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(process.leader_tick())

    assert runs == [("gc", 0.0, 0.01)]
    assert retention_store.calls == []
    assert "Leader task gc failed" in caplog.text

    clock.now = 30.0
    asyncio.run(process.leader_tick())

    assert len(runs) == 1
    assert retention_store.calls == ["runs", "blobs"]


def test_hung_retention_after_index_work_receives_only_remaining_budget_and_one_margin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(common_leader_tasks, "TICK_BUDGET_SECONDS", 0.06)
    monkeypatch.setattr(common_leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.02)
    clock = Clock()
    runs: _Runs = []
    index = Recorder("gc", clock, runs, cost=0.04, remains=True)
    retention_timeouts: list[float] = []
    captured_deadlines: list[float | None] = []
    real_timeout = asyncio.timeout

    def spy_timeout(delay: float | None) -> Any:
        captured_deadlines.append(delay)
        return real_timeout(delay)

    class _HangingRetention:
        async def run(self, budget_seconds: float) -> bool:
            retention_timeouts.append(budget_seconds)
            await asyncio.Event().wait()
            return True

    monkeypatch.setattr(asyncio, "timeout", spy_timeout)
    process = _worker_process(
        [
            ScheduledTask("gc", index, 300),
            ScheduledTask("run_data_retention", _HangingRetention(), INTERVAL_SECONDS),
        ],
        clock=clock,
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(process.leader_tick())

    assert runs == [("gc", 0.0, 0.06)]
    assert retention_timeouts == [pytest.approx(0.02)]
    assert captured_deadlines == [pytest.approx(0.08), pytest.approx(0.04)]
    assert "Leader task run_data_retention failed" in caplog.text


def test_retention_failure_timeout_and_cancellation_in_leader_tasks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()

    # Failure stays inside the tick and waits INTERVAL_SECONDS before retrying.
    failing_store = RetentionStore(clock, runs=(RuntimeError("database"), 0))
    failing_leader = LeaderTasks(
        [
            ScheduledTask(
                "run_data_retention",
                make_retention_task(failing_store, clock),
                INTERVAL_SECONDS,
            )
        ],
        clock=clock,
    )
    with caplog.at_level(logging.ERROR):
        asyncio.run(failing_leader.tick())
        asyncio.run(failing_leader.tick())
        assert failing_store.calls == ["runs"]
        clock.now += INTERVAL_SECONDS
        asyncio.run(failing_leader.tick())
    assert failing_store.calls == ["runs", "runs", "blobs"]
    assert "Leader task run_data_retention failed" in caplog.text

    # Timeout stays inside the tick and waits INTERVAL_SECONDS before retrying.
    monkeypatch.setattr(common_leader_tasks, "TICK_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(common_leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.01)
    hung_clock = Clock()
    hung_store = RetentionStore(hung_clock)
    hung_calls = 0

    async def hang(before: datetime, now: datetime, limit: int) -> int:
        nonlocal hung_calls
        hung_calls += 1
        await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(hung_store, "purge_expired_runs", hang)
    hung_leader = LeaderTasks(
        [
            ScheduledTask(
                "run_data_retention",
                make_retention_task(hung_store, hung_clock),
                INTERVAL_SECONDS,
            )
        ],
        clock=hung_clock,
    )
    asyncio.run(hung_leader.tick())
    asyncio.run(hung_leader.tick())
    assert hung_calls == 1

    # Cancellation propagates out of LeaderTasks.tick().
    cancelled_store = RetentionStore(hung_clock, runs=(asyncio.CancelledError(),))
    cancelled_leader = LeaderTasks(
        [
            ScheduledTask(
                "run_data_retention",
                make_retention_task(cancelled_store, hung_clock),
                INTERVAL_SECONDS,
            )
        ],
        clock=hung_clock,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_leader.tick())
