"""The webhook worker revives deferred installation deliveries on its hourly tick (api#71)."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, cast

import pytest

from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.webhook_worker import hourly_maintenance, revive_once, sweep_forever

_WORKER_LOGGER = "app.webhook_worker"


class Reviver:
    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = [] if calls is None else calls

    async def execute(self) -> int:
        self.calls.append("revive")
        return 2


class FailingReviver:
    async def execute(self) -> int:
        raise RuntimeError("database unavailable")


class Receiver:
    def __init__(self, calls: list[str], *, fails: bool = False) -> None:
        self.calls = calls
        self.fails = fails

    async def purge_finished(self) -> int:
        self.calls.append("purge")
        if self.fails:
            raise RuntimeError("database unavailable")
        return 1


def test_revive_once_returns_the_number_of_revived_deliveries() -> None:
    reviver = cast(ReviveDeferredInstallationDeliveries, Reviver())

    assert asyncio.run(revive_once(reviver)) == 2


def test_a_failed_revival_is_logged_with_its_exception_and_counts_as_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviver = cast(ReviveDeferredInstallationDeliveries, FailingReviver())

    with caplog.at_level(logging.ERROR, logger=_WORKER_LOGGER):
        revived = asyncio.run(revive_once(reviver))

    assert revived == 0
    [record] = [record for record in caplog.records if record.name == _WORKER_LOGGER]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "GitHub webhook deferred-delivery revival failed"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    assert str(record.exc_info[1]) == "database unavailable"


def test_the_hourly_tick_purges_finished_receipts_and_revives_deferred_ones() -> None:
    calls: list[str] = []
    receiver = cast(ReceiveGitHubDelivery, Receiver(calls))
    reviver = cast(ReviveDeferredInstallationDeliveries, Reviver(calls))

    asyncio.run(hourly_maintenance(receiver, reviver))

    assert calls == ["purge", "revive"]


def test_a_failed_purge_does_not_skip_the_revival() -> None:
    calls: list[str] = []
    receiver = cast(ReceiveGitHubDelivery, Receiver(calls, fails=True))
    reviver = cast(ReviveDeferredInstallationDeliveries, Reviver(calls))

    asyncio.run(hourly_maintenance(receiver, reviver))

    assert calls == ["purge", "revive"]


def test_the_worker_loop_runs_the_hourly_tick_with_its_reviver_on_the_first_iteration() -> None:
    async def scenario() -> list[str]:
        calls: list[str] = []
        waiting = asyncio.Event()

        class LoopReceiver(Receiver):
            async def replay_pending(self) -> int:
                calls.append("sweep")
                return 0

        class Wake(asyncio.Event):
            async def wait(self) -> Literal[True]:
                waiting.set()
                return await super().wait()

        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, LoopReceiver(calls)),
                cast(ReviveDeferredInstallationDeliveries, Reviver(calls)),
                Wake(),
            )
        )
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1)
            return calls
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert asyncio.run(scenario()) == ["sweep", "purge", "revive"]
