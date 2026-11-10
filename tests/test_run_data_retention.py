"""The run data retention setting and its schedule on the worker leader (#457)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast

import pytest

import app.common.application.leader_tasks as common_leader_tasks
from app.bootstrap.run_data_retention import RunDataRetentionSettings
from app.common.application.leader_tasks import LeaderTasks, ScheduledTask
from app.modules.reviews.application.run_data_retention import INTERVAL_SECONDS, PurgeExpiredRunData
from app.worker import WorkerProcess, WorkerSettings

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


_NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
_DAY = 86_400
_INTERVAL = 3_600


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Store:
    """Answers each batch from a script; 0 once it runs out. `cost` is the seconds per batch."""

    def __init__(
        self,
        clock: _Clock,
        *,
        runs: Sequence[int | BaseException] = (),
        blobs: Sequence[int | BaseException] = (),
        cost: float = 0.0,
    ) -> None:
        self._clock = clock
        self._runs = iter(runs)
        self._blobs = iter(blobs)
        self._cost = cost
        self.calls: list[str] = []
        self.commits = 0
        self.seen: list[tuple[Any, ...]] = []

    def _answer(self, answer: int | BaseException) -> int:
        self._clock.now += self._cost
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def purge_expired_runs(self, before: datetime, now: datetime, limit: int) -> int:
        self.calls.append("runs")
        self.seen.append((before, now, limit))
        return self._answer(next(self._runs, 0))

    async def clear_expired_blobs(self, now: datetime, limit: int) -> int:
        self.calls.append("blobs")
        self.seen.append((now, limit))
        return self._answer(next(self._blobs, 0))


class _Uow:
    def __init__(self, store: _Store) -> None:
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
        self.retention.commits += 1

    async def rollback(self) -> None:
        return None


def _task(store: _Store, clock: _Clock) -> PurgeExpiredRunData:
    return PurgeExpiredRunData(
        uow=lambda: _Uow(store),
        retention=timedelta(days=90),
        now=lambda: _NOW,
        clock=clock,
    )


def _leader(store: _Store, clock: _Clock) -> LeaderTasks:
    return LeaderTasks(
        [ScheduledTask("run_data_retention", _task(store, clock), INTERVAL_SECONDS)],
        clock=clock,
    )


def test_a_pass_drains_the_runs_then_the_blobs_one_transaction_per_batch() -> None:
    clock = _Clock()
    store = _Store(clock, runs=(100, 100, 3), blobs=(1_000, 7))

    assert asyncio.run(_task(store, clock).run(10)) is False

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
    clock = _Clock()
    store = _Store(clock, runs=runs, blobs=blobs, cost=6.0)

    assert asyncio.run(_task(store, clock).run(10)) is True

    assert store.calls == calls


def test_a_tick_runs_at_once_and_then_once_per_interval() -> None:
    clock = _Clock()
    store = _Store(clock)
    task = _leader(store, clock)

    async def ticks() -> None:
        await task.tick()
        assert store.calls == ["runs", "blobs"]
        clock.now += _INTERVAL - 1
        await task.tick()
        assert store.calls == ["runs", "blobs"]
        clock.now += 1
        await task.tick()

    asyncio.run(ticks())

    assert store.calls == ["runs", "blobs", "runs", "blobs"]


def test_a_tick_cut_off_by_its_budget_goes_on_at_the_next_tick() -> None:
    clock = _Clock()
    store = _Store(clock, runs=(100, 100), cost=_DAY)
    task = _leader(store, clock)

    async def ticks() -> None:
        await task.tick()
        assert store.calls == ["runs"]
        await task.tick()

    asyncio.run(ticks())

    assert store.calls == ["runs", "runs"]


def test_a_failed_pass_stays_inside_the_tick_and_waits_for_the_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    store = _Store(clock, runs=(RuntimeError("database"), 0))
    task = _leader(store, clock)

    async def ticks() -> None:
        await task.tick()
        await task.tick()
        assert store.calls == ["runs"]
        clock.now += _INTERVAL
        await task.tick()

    with caplog.at_level(logging.ERROR):
        asyncio.run(ticks())

    assert store.calls == ["runs", "runs", "blobs"]
    assert store.commits == 2
    assert "Leader task run_data_retention failed" in caplog.text


def test_a_hung_pass_times_out_inside_the_tick_and_waits_for_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common_leader_tasks, "TICK_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(common_leader_tasks, "TASK_TIMEOUT_MARGIN_SECONDS", 0.01)
    clock = _Clock()
    store = _Store(clock)
    task = _leader(store, clock)
    calls = 0

    async def hang(before: datetime, now: datetime, limit: int) -> int:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(store, "purge_expired_runs", hang)

    async def ticks() -> None:
        await task.tick()
        await task.tick()

    asyncio.run(ticks())

    assert calls == 1


def test_a_cancelled_pass_cancels_the_tick() -> None:
    clock = _Clock()
    store = _Store(clock, runs=(asyncio.CancelledError(),))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_leader(store, clock).tick())


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
