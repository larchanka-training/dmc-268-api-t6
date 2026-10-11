"""The webhook sweep consumes queue hints without losing races or its fallback (#129)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Literal, Self, cast

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import webhook_worker
from app.common.infrastructure.heartbeat import is_fresh
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.webhook_worker import sweep_forever


class Wake(asyncio.Event):
    """Observe the public wait boundary without sleeps or the loop's internal state."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting: asyncio.Queue[None] = asyncio.Queue()

    async def wait(self) -> Literal[True]:
        self.waiting.put_nowait(None)
        return await super().wait()


class Receiver:
    def __init__(self, *, first_batch: int = 0) -> None:
        self.first_batch = first_batch
        self.count = 0
        self.scans: asyncio.Queue[int] = asyncio.Queue()
        self.purges = 0

    async def replay_pending(self) -> int:
        self.count += 1
        self.scans.put_nowait(self.count)
        return self.first_batch if self.count == 1 else 0

    async def purge_finished(self) -> int:
        self.purges += 1
        return 0


class Reviver:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self) -> int:
        self.calls += 1
        return 0


async def cancel(task: asyncio.Task[object]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_startup_scan_and_idle_notification_happen_before_polling_deadline() -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            assert receiver.purges == reviver.calls == 1
            wake.set()
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_signal_during_dispatch_survives_transition_back_to_waiting() -> None:
    async def scenario() -> None:
        wake, entered, release = Wake(), asyncio.Event(), asyncio.Event()
        wake.set()  # A pre-start hint must be consumed by the immediate scan.

        class BusyReceiver(Receiver):
            async def replay_pending(self) -> int:
                assert wake.is_set() is False
                if self.count == 0:
                    entered.set()
                    await release.wait()
                return await super().replay_pending()

        receiver = BusyReceiver()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, Reviver()),
                wake,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            wake.set()
            release.set()
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_fallback_timeout_scans_again_without_a_notification() -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
                poll_interval=0.02,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            assert wake.is_set() is False
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
            assert receiver.purges == reviver.calls == 1
        finally:
            await cancel(task)

    asyncio.run(scenario())


@pytest.mark.parametrize(("batch", "continues"), [(99, False), (100, True), (101, False)])
def test_only_exact_full_batch_continues_immediately_and_yields_to_other_tasks(
    batch: int, continues: bool
) -> None:
    async def scenario() -> None:
        wake, other_task_ran = Wake(), asyncio.Event()

        class FairReceiver(Receiver):
            async def replay_pending(self) -> int:
                if self.count == 1:
                    assert other_task_ran.is_set() is True
                return await super().replay_pending()

        receiver = FairReceiver(first_batch=batch)
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, Reviver()),
                wake,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            other_task_ran.set()
            if continues:
                assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
            else:
                await asyncio.wait_for(wake.waiting.get(), timeout=1)
                assert receiver.count == 1
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_frequent_notifications_keep_hourly_maintenance_on_monotonic_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = [100.0]
        wake = Wake()
        purges: list[float] = []
        revivals: list[float] = []
        monkeypatch.setattr(webhook_worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))

        class NoisyReceiver(Receiver):
            async def replay_pending(self) -> int:
                clock[0] = [100.0, 101.0, 3699.0, 3700.0, 3701.0][self.count]
                if self.count < 4:
                    wake.set()
                return await super().replay_pending()

            async def purge_finished(self) -> int:
                purges.append(clock[0])
                return 0

        class TimedReviver(Reviver):
            async def execute(self) -> int:
                revivals.append(clock[0])
                return 0

        receiver = NoisyReceiver()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, TimedReviver()),
                wake,
            )
        )
        try:
            for expected in range(1, 6):
                assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == expected
            assert purges == [100.0, 3700.0]
            assert revivals == [100.0, 3700.0]
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_worker_starts_scan_and_heartbeat_before_listener_ready_and_awaits_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        database_attempted = asyncio.Event()
        listening, register, receiving = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closing, finish_close = asyncio.Event(), asyncio.Event()
        closed: list[str] = []
        listener_connections: list[str] = []
        heartbeat = tmp_path / "webhook-worker.heartbeat"
        messages: asyncio.Queue[psycopg.Notify] = asyncio.Queue()

        class ListenerConnection:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                closing.set()
                await finish_close.wait()
                closed.append("listener")

            async def execute(self, statement: str) -> None:
                assert statement == "LISTEN webhook_work_available"
                listening.set()
                await register.wait()

            async def notifies(self) -> AsyncIterator[psycopg.Notify]:
                receiving.set()
                while True:
                    yield await messages.get()

        async def connect(conninfo: str = "", **kwargs: object) -> ListenerConnection:
            if kwargs.get("autocommit") is True:
                listener_connections.append(conninfo)
                return ListenerConnection()
            database_attempted.set()
            raise psycopg.OperationalError("synthetic queue database outage")

        class Publisher:
            def __init__(self, url: str) -> None:
                assert url == "amqp://synthetic-broker/"

            async def aclose(self) -> None:
                assert closed == ["listener"]
                closed.append("publisher")

        real_dispose = AsyncEngine.dispose

        async def dispose(engine: AsyncEngine, close: bool = True) -> None:
            assert closed == ["listener", "publisher"]
            await real_dispose(engine, close=close)
            closed.append("database")

        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
        monkeypatch.setattr(webhook_worker, "LazyAmqpPublisher", Publisher)
        monkeypatch.setattr(AsyncEngine, "dispose", dispose)
        for name, value in {
            "DATABASE_URL": "postgresql+psycopg://app:synthetic@127.0.0.1:1/app",
            "GITHUB_APP_ID": "17",
            "GITHUB_APP_PRIVATE_KEY": "synthetic-private-key",
            "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
            "RABBITMQ_URL": "amqp://synthetic-broker/",
            "WORKER_HEARTBEAT_FILE": str(heartbeat),
        }.items():
            monkeypatch.setenv(name, value)
        task = asyncio.create_task(webhook_worker.run_forever())
        try:
            await asyncio.wait_for(listening.wait(), timeout=2)
            await asyncio.wait_for(database_attempted.wait(), timeout=2)
            assert is_fresh(heartbeat, max_age=5, now=time.time()) is True
            assert task.done() is False
            register.set()
            await asyncio.wait_for(receiving.wait(), timeout=2)
            for _ in range(3):
                messages.put_nowait(psycopg.Notify("webhook_work_available", "", 123))
            assert len(listener_connections) == 1
            task.cancel()
            await asyncio.wait_for(closing.wait(), timeout=2)
            assert closed == []
            finish_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert closed == ["listener", "publisher", "database"]
        finally:
            finish_close.set()
            if not task.done():
                await cancel(task)

    asyncio.run(scenario())
