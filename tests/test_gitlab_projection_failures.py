"""Projection operation failures remain observable without exposing driver payloads."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.request_credentials import GitLabRequestUnavailable
from app.modules.reviews.infrastructure import gitlab_merge_request_projection as projection
from tests.test_gitlab_merge_request_projection import COMMAND, CURRENT, NOW

CANARY = "postgresql://user:PAT-secret@host/database SELECT private_body"


def assert_safe_records(caplog: pytest.LogCaptureFixture, count: int) -> None:
    records = [r for r in caplog.records if r.name == projection.__name__]
    assert len(records) == count
    for record in records:
        assert record.levelno == logging.ERROR
        assert record.getMessage() == "GitLab projection database operation failed"
        assert record.__dict__["error_type"] == "RuntimeError"
        assert record.exc_info is None and record.stack_info is None
    assert CANARY not in caplog.text


@pytest.mark.parametrize("operation", ["write", "commit"])
def test_projection_data_operations_emit_only_safe_error_class(
    operation: str, caplog: pytest.LogCaptureFixture
) -> None:
    actions: list[str] = []

    class Session:
        async def scalar(self, statement: object) -> object:
            raise RuntimeError(CANARY)

        async def commit(self) -> None:
            raise RuntimeError(CANARY)

        async def rollback(self) -> None:
            actions.append("rollback")

        async def close(self) -> None:
            actions.append("close")

    async def scenario() -> None:
        with pytest.raises(
            GitLabRequestUnavailable, match="^gitlab_request_unavailable$"
        ) as caught:
            if operation == "write":
                store = projection.SqlAlchemyGitLabMergeRequestProjectionStore(cast(Any, Session()))
                await store.get_or_create_locked(CURRENT, NOW)
            else:
                factory = cast(async_sessionmaker[AsyncSession], lambda: Session())
                async with projection.SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(
                    factory
                ) as uow:
                    await uow.commit()
        assert caught.value.__cause__ is None and caught.value.__suppress_context__

    asyncio.run(scenario())
    assert actions == (["rollback", "close"] if operation == "commit" else [])
    assert_safe_records(caplog, 1)


@pytest.mark.parametrize(
    "faults,expected",
    [
        ({"connect"}, ["connect"]),
        ({"set_timeout"}, ["connect", "set_timeout", "rollback", "reset", "close"]),
        ({"acquire"}, ["connect", "set_timeout", "acquire", "rollback", "reset", "close"]),
        ({"rollback"}, ["connect", "set_timeout", "acquire", "rollback", "invalidate", "close"]),
        (
            {"unlock"},
            ["connect", "set_timeout", "acquire", "rollback", "unlock", "invalidate", "close"],
        ),
        (
            {"reset"},
            [
                "connect",
                "set_timeout",
                "acquire",
                "rollback",
                "unlock",
                "reset",
                "invalidate",
                "close",
            ],
        ),
        (
            {"close"},
            [
                "connect",
                "set_timeout",
                "acquire",
                "rollback",
                "unlock",
                "reset",
                "close",
                "invalidate",
            ],
        ),
        (
            {"reset", "invalidate"},
            [
                "connect",
                "set_timeout",
                "acquire",
                "rollback",
                "unlock",
                "reset",
                "invalidate",
                "close",
            ],
        ),
    ],
)
def test_projection_lock_logs_driver_faults_once_and_keeps_cleanup_attempts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    faults: set[str],
    expected: list[str],
) -> None:
    actions: list[str] = []

    def step(name: str) -> None:
        actions.append(name)
        if name in faults:
            raise RuntimeError(CANARY)

    class Connection:
        async def execute(self, statement: object, *args: object) -> None:
            sql = str(statement)
            step(
                "set_timeout"
                if "set_config" in sql
                else "unlock"
                if "unlock" in sql
                else "reset"
                if sql.startswith("RESET")
                else "acquire"
            )

        async def rollback(self) -> None:
            step("rollback")

        async def invalidate(self) -> None:
            step("invalidate")

        async def close(self) -> None:
            step("close")

    class Engine:
        async def connect(self) -> Connection:
            step("connect")
            return Connection()

    monkeypatch.setattr(projection, "create_async_engine", lambda *a, **kw: Engine())

    async def scenario() -> None:
        lock = projection.SqlAlchemyGitLabMergeRequestProjectionLock("unused")
        with pytest.raises(
            GitLabRequestUnavailable, match="^gitlab_request_unavailable$"
        ) as caught:
            async with lock.hold(COMMAND):
                pass
        assert caught.value.__cause__ is None

    asyncio.run(scenario())
    assert actions == expected
    assert_safe_records(caplog, len(faults))


@pytest.mark.parametrize("cancelled", [False, True])
def test_projection_yielded_primary_survives_cleanup_fault_without_application_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cancelled: bool,
) -> None:
    class Connection:
        async def execute(self, statement: object, *args: object) -> None:
            if str(statement).startswith("RESET"):
                raise RuntimeError(CANARY)

        async def rollback(self) -> None:
            pass

        async def invalidate(self) -> None:
            pass

        async def close(self) -> None:
            pass

    class Engine:
        async def connect(self) -> Connection:
            return Connection()

    monkeypatch.setattr(projection, "create_async_engine", lambda *a, **kw: Engine())
    primary = (
        asyncio.CancelledError("body cancellation") if cancelled else LookupError("body failure")
    )

    async def scenario() -> None:
        lock = projection.SqlAlchemyGitLabMergeRequestProjectionLock("unused")
        with pytest.raises(type(primary)) as caught:
            async with lock.hold(COMMAND):
                raise primary
        assert caught.value is primary

    asyncio.run(scenario())
    assert_safe_records(caplog, 1)
