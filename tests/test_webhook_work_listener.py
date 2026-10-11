"""Webhook LISTEN lifecycle at the psycopg system boundary (#129)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Self

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

DATABASE_URL = (
    "postgresql+psycopg://app:synthetic%40pass@localhost:5434/app"
    "?options=-csearch_path%3Dlistener_test&sslmode=require"
)


class Connection:
    """Controlled third-party connection, including registration and notification barriers."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.executed: list[str] = []
        self.listening = asyncio.Event()
        self.register = asyncio.Event()
        self.messages: asyncio.Queue[psycopg.Notify | Exception] = asyncio.Queue()
        self.consumed: asyncio.Queue[None] = asyncio.Queue()
        self.closed = asyncio.Event()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed.set()

    async def execute(self, statement: str) -> None:
        self.executed.append(statement)
        self.listening.set()
        await self.register.wait()
        if self.error is not None:
            raise self.error

    async def notifies(self) -> AsyncIterator[psycopg.Notify]:
        while True:
            message = await self.messages.get()
            if isinstance(message, Exception):
                raise message
            yield message
            self.consumed.put_nowait(None)


class Connector:
    def __init__(self, *connections: Connection | Exception) -> None:
        self.connections: asyncio.Queue[Connection | Exception] = asyncio.Queue()
        self.connecting = asyncio.Event()
        for connection in connections:
            self.connections.put_nowait(connection)
        self.calls: list[tuple[str, bool]] = []

    async def connect(self, conninfo: str, *, autocommit: bool) -> Connection:
        self.calls.append((conninfo, autocommit))
        self.connecting.set()
        connection = await self.connections.get()
        if isinstance(connection, Exception):
            raise connection
        return connection


async def cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_registration_and_notifications_wake_event_and_cancel_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        from app.bootstrap.webhook_work_listener import listen_forever

        connection = Connection()
        connector = Connector(connection)
        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connector.connect)
        wake = asyncio.Event()
        task = asyncio.create_task(listen_forever(DATABASE_URL, wake))
        try:
            await asyncio.wait_for(connection.listening.wait(), timeout=2)
            assert wake.is_set() is False
            connection.register.set()
            await asyncio.wait_for(wake.wait(), timeout=2)
            assert connection.executed == ["LISTEN webhook_work_available"]
            [(conninfo, autocommit)] = connector.calls
            assert autocommit is True
            assert conninfo_to_dict(conninfo) == {
                "user": "app",
                "password": "synthetic@pass",
                "host": "localhost",
                "port": "5434",
                "dbname": "app",
                "options": "-csearch_path=listener_test",
                "sslmode": "require",
            }
            wake.clear()
            for _ in range(3):
                connection.messages.put_nowait(psycopg.Notify("webhook_work_available", "", 123))
                await asyncio.wait_for(connection.consumed.get(), timeout=2)
            assert wake.is_set() is True
        finally:
            await cancel(task)
        assert connection.closed.is_set() is True

    asyncio.run(scenario())


def test_connection_loss_reconnects_and_registered_connection_forces_scan(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        from app.bootstrap.webhook_work_listener import listen_forever

        first, second = Connection(), Connection()
        first.register.set()
        connector = Connector(first, second)
        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connector.connect)
        wake = asyncio.Event()
        task = asyncio.create_task(listen_forever(DATABASE_URL, wake, retry=0.01))
        try:
            await asyncio.wait_for(wake.wait(), timeout=2)
            wake.clear()
            first.messages.put_nowait(psycopg.OperationalError("synthetic connection loss"))
            await asyncio.wait_for(second.listening.wait(), timeout=2)
            assert first.closed.is_set() is True
            assert wake.is_set() is False
            second.register.set()
            await asyncio.wait_for(wake.wait(), timeout=2)
            assert second.executed == ["LISTEN webhook_work_available"]
            assert len(connector.calls) == 2
        finally:
            await cancel(task)
        assert second.closed.is_set() is True

    asyncio.run(scenario())
    records = [
        record for record in caplog.records if record.name == "app.bootstrap.webhook_work_listener"
    ]
    assert len(records) == 1
    assert (
        records[0].getMessage()
        == "webhook_work_available listener lost its connection; reconnecting"
    )
    assert records[0].exc_info is not None


@pytest.mark.parametrize("failure_stage", ["connect", "listen"])
def test_initial_connection_or_registration_failure_retries_until_ready(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
) -> None:
    async def scenario() -> None:
        from app.bootstrap.webhook_work_listener import listen_forever

        error = psycopg.OperationalError("synthetic setup failure")
        first = Connection(error)
        first.register.set()
        ready = Connection()
        ready.register.set()
        connector = Connector(error if failure_stage == "connect" else first, ready)
        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connector.connect)
        wake = asyncio.Event()
        task = asyncio.create_task(listen_forever(DATABASE_URL, wake, retry=0.01))
        try:
            await asyncio.wait_for(wake.wait(), timeout=2)
            assert ready.executed == ["LISTEN webhook_work_available"]
            assert len(connector.calls) == 2
            if failure_stage == "listen":
                assert first.closed.is_set() is True
        finally:
            await cancel(task)
        assert ready.closed.is_set() is True

    asyncio.run(scenario())
    records = [
        record for record in caplog.records if record.name == "app.bootstrap.webhook_work_listener"
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None


def test_cancellation_during_reconnect_delay_is_prompt_and_does_not_connect_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        from app.bootstrap.webhook_work_listener import listen_forever

        connection = Connection()
        connection.register.set()
        connector = Connector(connection)
        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connector.connect)
        wake = asyncio.Event()
        task = asyncio.create_task(listen_forever(DATABASE_URL, wake, retry=60))
        try:
            await asyncio.wait_for(wake.wait(), timeout=2)
            connection.messages.put_nowait(psycopg.OperationalError("synthetic connection loss"))
            await asyncio.wait_for(connection.closed.wait(), timeout=2)
        finally:
            # The long injected backoff must be cancellable well before its deadline.
            await asyncio.wait_for(cancel(task), timeout=2)
        assert len(connector.calls) == 1
        assert connection.closed.is_set() is True

    asyncio.run(scenario())


def test_cancellation_during_registration_closes_connection_without_readiness_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        from app.bootstrap.webhook_work_listener import listen_forever

        connection = Connection()
        connector = Connector(connection)
        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connector.connect)
        wake = asyncio.Event()
        task = asyncio.create_task(listen_forever(DATABASE_URL, wake))
        try:
            await asyncio.wait_for(connection.listening.wait(), timeout=2)
        finally:
            await asyncio.wait_for(cancel(task), timeout=2)
        assert wake.is_set() is False
        assert connection.closed.is_set() is True
        assert len(connector.calls) == 1

    asyncio.run(scenario())


def test_cancellation_during_connection_setup_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        from app.bootstrap.webhook_work_listener import listen_forever

        connector = Connector()
        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connector.connect)
        wake = asyncio.Event()
        task = asyncio.create_task(listen_forever(DATABASE_URL, wake))
        try:
            await asyncio.wait_for(connector.connecting.wait(), timeout=2)
        finally:
            await asyncio.wait_for(cancel(task), timeout=2)
        assert wake.is_set() is False
        assert len(connector.calls) == 1

    asyncio.run(scenario())
