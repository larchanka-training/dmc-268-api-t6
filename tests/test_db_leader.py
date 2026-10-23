"""Leader retries preserve cancellation even when connection/tick cleanup masks it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Self, cast

import psycopg
import pytest
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.common.infrastructure.db.leader import run_as_leader


@dataclass
class Engine:
    phase: Literal["connection", "tick"]
    mode: Literal["replace_cancel", "error_once"]
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    retried: asyncio.Event = field(default_factory=asyncio.Event)
    connections: int = 0
    calls: int = 0
    invalidated: int = 0
    error: ProgrammingError = field(
        default_factory=lambda: ProgrammingError(
            "dialect initialization",
            {},
            psycopg.ProgrammingError("Explicit rollback() forbidden within a Transaction context"),
        )
    )

    def connect(self) -> Connection:
        self.connections += 1
        return Connection(self)

    async def operation(self, phase: str) -> None:
        if phase != self.phase:
            return
        self.calls += 1
        if self.mode == "error_once" and self.calls == 1:
            raise self.error
        if self.mode == "error_once":
            self.retried.set()
        else:
            self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if self.mode == "replace_cancel":
                raise self.error from None
            raise

    async def tick(self) -> None:
        await self.operation("tick")


class Connection:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def __aenter__(self) -> Self:
        await self.engine.operation("connection")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def execution_options(self, *, isolation_level: str) -> Self:
        assert isolation_level == "AUTOCOMMIT"
        return self

    async def scalar(self, statement: object, parameters: object = None) -> bool:
        return True

    async def invalidate(self) -> None:
        self.engine.invalidated += 1


@pytest.mark.parametrize("phase", ["connection", "tick"])
def test_cleanup_error_after_cancel_does_not_restart_leader(
    phase: Literal["connection", "tick"], caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        engine = Engine(phase, "replace_cancel")
        task = asyncio.create_task(
            run_as_leader(cast(AsyncEngine, engine), 1, 30, engine.tick, name="test")
        )
        try:
            await asyncio.wait_for(engine.entered.wait(), 1)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done, "leader swallowed cancellation and entered its retry delay"
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.__cause__ is engine.error
            assert task.cancelling() == 1
            assert engine.calls == 1 and engine.connections == 1
            assert engine.invalidated == (1 if phase == "tick" else 0)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert "Explicit rollback() forbidden" in caplog.text


@pytest.mark.parametrize("phase", ["connection", "tick"])
def test_unrelated_leader_error_is_retried_and_normal_cancel_still_propagates(
    phase: Literal["connection", "tick"], caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        engine = Engine(phase, "error_once")
        task = asyncio.create_task(
            run_as_leader(cast(AsyncEngine, engine), 1, 0, engine.tick, name="test")
        )
        try:
            await asyncio.wait_for(engine.retried.wait(), 1)
            assert engine.calls == 2
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelling() == 1
            assert engine.calls == 2
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert "Explicit rollback() forbidden" in caplog.text
