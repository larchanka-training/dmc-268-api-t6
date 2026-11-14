"""The run data retention setting and its schedule on the worker leader (#457)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.bootstrap.run_data_retention import RunDataRetentionSettings
from app.common.application.leader_tasks import LeaderTasks, ScheduledTask
from app.modules.reviews.application.run_data_retention import INTERVAL_SECONDS
from app.worker import WorkerProcess, WorkerSettings
from tests.run_data_retention_fakes import (
    DEFAULT_RETENTION_NOW as _NOW,
)
from tests.run_data_retention_fakes import (
    RetentionClock,
    RetentionStore,
    make_retention_task,
)

_WORKER_ENV = {
    "DATABASE_URL": "postgresql+psycopg://unused/unused",
    "RABBITMQ_URL": "amqp://unused/",
}


@pytest.mark.parametrize(
    ("env", "days"),
    [
        pytest.param({}, 90, id="unset"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": ""}, 90, id="empty"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": "1"}, 1, id="lowest"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": " 30 "}, 30, id="spaces"),
        pytest.param({"RUN_DATA_RETENTION_DAYS": "3650"}, 3650, id="highest"),
    ],
)
def test_the_retention_period_comes_from_the_environment(env: dict[str, str], days: int) -> None:
    assert RunDataRetentionSettings.from_environment(env).days == days


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0", id="zero"),
        pytest.param("3651", id="above-ten-years"),
        pytest.param("-1", id="negative"),
        pytest.param("1.5", id="fraction"),
        pytest.param("three months", id="words"),
        pytest.param("٩٠", id="non-ascii-digits"),
        pytest.param("1" * 19, id="too-many-digits"),
        pytest.param(" ", id="blank"),
    ],
)
def test_an_invalid_retention_period_stops_the_worker_start(value: str) -> None:
    with pytest.raises(ValueError, match="RUN_DATA_RETENTION_DAYS"):
        WorkerSettings.from_environment({**_WORKER_ENV, "RUN_DATA_RETENTION_DAYS": value})


def test_the_worker_settings_read_the_retention_period() -> None:
    assert WorkerSettings.from_environment(_WORKER_ENV).run_data_retention.days == 90
    settings = WorkerSettings.from_environment({**_WORKER_ENV, "RUN_DATA_RETENTION_DAYS": "45"})
    assert settings.run_data_retention.days == 45


_DAY = 86_400


def _leader(store: RetentionStore, clock: RetentionClock) -> LeaderTasks:
    return LeaderTasks(
        [ScheduledTask("run_data_retention", make_retention_task(store, clock), INTERVAL_SECONDS)],
        clock=clock,
    )


def test_a_pass_drains_the_runs_then_the_blobs_one_transaction_per_batch() -> None:
    clock = RetentionClock(1_000.0)
    store = RetentionStore(clock, runs=(100, 100, 3), blobs=(1_000, 7))

    assert asyncio.run(make_retention_task(store, clock).run(10)) is False

    assert store.calls == ["runs", "runs", "runs", "blobs", "blobs"]
    assert store.commits == 5
    assert store.seen[0] == (datetime(2026, 8, 8, 12, tzinfo=UTC), _NOW, 100)
    assert store.seen[-1] == (_NOW, 1_000)


@pytest.mark.parametrize(
    ("runs", "blobs", "calls"),
    [
        pytest.param((100, 100, 100), (), ["runs", "runs"], id="in-the-runs"),
        pytest.param((3,), (1_000, 1_000), ["runs", "blobs"], id="in-the-blobs"),
    ],
)
def test_a_pass_reports_the_work_its_budget_cut_off(
    runs: tuple[int, ...], blobs: tuple[int, ...], calls: list[str]
) -> None:
    clock = RetentionClock(1_000.0)
    store = RetentionStore(clock, runs=runs, blobs=blobs, cost=6.0)

    assert asyncio.run(make_retention_task(store, clock).run(10)) is True

    assert store.calls == calls


def test_retention_runs_and_resumes_through_leader_tasks() -> None:
    clock = RetentionClock(1_000.0)
    store = RetentionStore(clock, runs=(100,), cost=6.0)
    leader = _leader(store, clock)

    async def ticks() -> None:
        # Tick 1 finishes `runs` (`100`, then `0`), hits deadline at 1012s before `blobs`,
        # so the gate stays open for an immediate resume on Tick 2 without advancing clock.
        await leader.tick()
        assert store.calls == ["runs", "runs"]
        # Once drained, the hourly interval gate measures INTERVAL_SECONDS from tick start.
        tick2_start = clock.now
        await leader.tick()
        assert store.calls == ["runs", "runs", "runs", "blobs"]
        clock.now = tick2_start + INTERVAL_SECONDS - 1
        await leader.tick()
        assert store.calls == ["runs", "runs", "runs", "blobs"]
        clock.now = tick2_start + INTERVAL_SECONDS
        await leader.tick()

    asyncio.run(ticks())

    assert store.calls == ["runs", "runs", "runs", "blobs", "runs", "blobs"]


class _Leader:
    def __init__(self, failing: str | None = None) -> None:
        self.calls: list[str] = []
        self._failing = failing

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name == self._failing:
            raise RuntimeError(f"{name} failed")

    async def execute(self) -> None:
        self._call("sweep")

    async def replay_pending_publications(self) -> None:
        self._call("replay")

    async def tick(self) -> None:
        self._call("retention")

    def process(self) -> WorkerProcess:
        return WorkerProcess(
            handle_run=cast(Any, None),
            handle_delivery=cast(Any, None),
            publish_review=cast(Any, None),
            enqueue=cast(Any, self),
            sweep=cast(Any, self),
            leader_tasks=cast(Any, self),
        )


def test_the_worker_leader_runs_the_retention_after_the_outbox_replay() -> None:
    leader = _Leader()

    asyncio.run(leader.process().leader_tick())

    assert leader.calls == ["sweep", "replay", "retention"]


def test_the_worker_leader_runs_the_retention_even_when_the_replay_fails() -> None:
    leader = _Leader(failing="replay")

    with pytest.raises(RuntimeError, match="replay failed"):
        asyncio.run(leader.process().leader_tick())

    assert leader.calls == ["sweep", "replay", "retention"]
