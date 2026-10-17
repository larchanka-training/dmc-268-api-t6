"""Run lifecycle writes wait behind a concurrent origin revoke or rebind, then cancel."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import INSTALL_A, INSTALL_B, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_app_reinstall_postgres import wait_for_blocked_backend
from tests.test_run_context_origin_postgres import context_row
from tests.test_run_diff_origin_postgres import history, history_in
from tests.test_run_origin_guard_postgres import prepare

COMPETITORS = ["revoke", "rebind"]
FINDING = UUID(int=16401)
# state, error_code, error_message, lease_until, finished_at set
CANCELLED = ("cancelled", "installation_unavailable", None, None, True)


@pytest.fixture
def concurrency_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def own(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE runs SET worker_id='owner',"
                "lease_until=clock_timestamp()+interval '1 hour' WHERE id=:id"
            ),
            {"id": RUN_ATTEMPTED},
        )


async def snapshot(factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    return await history(factory) | {"context_payload": await context_row(factory)}


def without_run(state: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in state.items() if key != "run"}


def outcome(state: dict[str, object]) -> tuple[object, ...]:
    run = state["run"]
    assert isinstance(run, dict)
    return (
        run["state"],
        run["error_code"],
        run["error_message"],
        run["lease_until"],
        run["finished_at"] is not None,
    )


async def race[T](
    factory: async_sessionmaker[AsyncSession],
    competitor: str,
    operation: Callable[[], Coroutine[Any, Any, T]],
) -> tuple[asyncio.Task[T], dict[str, object]]:
    """Start operation while competitor holds only the origin installation, then retire it.

    Returns the finished task and the state the operation must not alter, read
    after the competitor's writes and before its commit.

    The rebind competitor models rebind's lock order and its origin change only; it does
    not cancel the Run with installation_replaced or soft-delete the source installation.
    So it proves lock order plus the origin guard, not production rebind outcomes.
    """
    task: asyncio.Task[T] | None = None
    try:
        # Revoke and rebind both run in this UoW (installation_repository_unit_of_work).
        async with (
            SqlAlchemyInstallationRepositoriesUnitOfWork(factory) as competing,
            factory() as observer,
        ):
            session = competing.session
            if competitor == "revoke":
                # Installation FOR NO KEY UPDATE only (installation_repository_unit_of_work:214).
                await competing.repositories._record_revocation(INSTALL_A, 0)
            else:
                # Lock order of rebind, not its outcomes: source installations
                # FOR NO KEY UPDATE (github_repository_rebinding:30-40).
                await session.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR NO KEY UPDATE"),
                    {"id": INSTALL_A},
                )
            blocker_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(blocker_pid, int)
            task = asyncio.create_task(operation())
            # A real PostgreSQL lock edge, not an event on the helper's first query:
            # the operation must already wait before the competitor touches Repository.
            await wait_for_blocked_backend(observer, blocker_pid, task)
            async with asyncio.timeout(10):
                if competitor == "revoke":
                    # Its repositories UPDATE is revoke's Repository row lock (L190-208).
                    await competing.repositories.disable_installation(INSTALL_A)
                else:
                    # Repository FOR UPDATE, Run lock, then the FK move, as in
                    # github_repository_rebinding:86-157. The Run is only locked, not cancelled
                    # with installation_replaced, and the source gets no deleted_at: the origin
                    # guard alone must cancel.
                    await session.execute(
                        text("SELECT id FROM repositories WHERE id=:id FOR UPDATE"), {"id": REPO_A}
                    )
                    await session.execute(
                        text("SELECT id FROM runs WHERE id=:id FOR UPDATE"), {"id": RUN_ATTEMPTED}
                    )
                    await session.execute(
                        text("UPDATE repositories SET provider_installation_id=:to WHERE id=:id"),
                        {"to": INSTALL_B, "id": REPO_A},
                    )
                before = await history_in(session) | {"context_payload": await context_row(factory)}
                await competing.commit()
    finally:
        # The competitor has committed or rolled back: the operation can always finish.
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    return task, before


@pytest.mark.integration
@pytest.mark.parametrize("competitor", COMPETITORS)
def test_finish_waits_for_revoke_or_rebind_then_cancels_the_owned_attempt(
    concurrency_env: Env, competitor: str
) -> None:
    async def exercise() -> None:
        engine = concurrency_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def finish() -> bool:
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                changed = await uow.runs.finish(
                    RUN_ATTEMPTED,
                    from_state="running",
                    worker_id="owner",
                    state="failed",
                    error_code="diff_fetch_failed",
                    error_message="provider failure",
                    now=datetime.now(UTC),
                )
                await uow.commit()
            return changed

        try:
            await prepare(factory, "active", "running")
            await own(factory)
            task, _ = await race(factory, competitor, finish)
            assert task.result() is True
            after = await snapshot(factory)
            assert outcome(after) == CANCELLED
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("competitor", COMPETITORS)
def test_requeue_waits_for_revoke_or_rebind_then_cancels_instead_of_queueing(
    concurrency_env: Env, competitor: str
) -> None:
    async def exercise() -> None:
        engine = concurrency_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def requeue() -> bool:
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                requeued = await uow.runs.requeue(
                    RUN_ATTEMPTED,
                    worker_id="owner",
                    available_at=datetime.now(UTC) + timedelta(minutes=10),
                )
                await uow.commit()
            return requeued

        try:
            await prepare(factory, "active", "running")
            await own(factory)
            task, _ = await race(factory, competitor, requeue)
            assert task.result() is False
            after = await snapshot(factory)
            assert outcome(after) == CANCELLED
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("competitor", COMPETITORS)
def test_complete_publication_waits_for_revoke_or_rebind_then_cancels_without_payload_writes(
    concurrency_env: Env, competitor: str
) -> None:
    async def exercise() -> None:
        engine = concurrency_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def complete() -> str | None:
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                # An active origin would mark the finding published and record comment 900.
                completed = await uow.runs.complete_publication(
                    RUN_ATTEMPTED,
                    review_id=55,
                    comment_ids=(900,),
                    findings_hash="f" * 64,
                    moved_to_body=False,
                    now=datetime.now(UTC),
                )
                await uow.commit()
            return completed

        try:
            await prepare(factory, "active", "publishing")
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO findings "
                        "(id,run_id,file_path,line_start,severity,confidence,category,title,body,"
                        "published,inline_comment) VALUES "
                        "(:id,:run,'a.py',3,'medium',0.8,'correctness','T','B',false,true)"
                    ),
                    {"id": FINDING, "run": RUN_ATTEMPTED},
                )
            task, before = await race(factory, competitor, complete)
            assert task.result() == "cancelled"
            after = await snapshot(factory)
            assert outcome(after) == CANCELLED
            assert without_run(after) == without_run(before)
            assert after["comments"] == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("competitor", COMPETITORS)
def test_save_context_payload_waits_for_revoke_or_rebind_then_refuses_the_write(
    concurrency_env: Env, competitor: str
) -> None:
    async def exercise() -> None:
        engine = concurrency_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
        summary: dict[str, Any] = {"late": True}
        try:
            await prepare(factory, "active", "running")
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO context_payloads(id,run_id,schema_version,summary) "
                        "VALUES (:id,:id,1,'{\"preserve\": true}')"
                    ),
                    {"id": RUN_ATTEMPTED},
                )
            task, before = await race(
                factory, competitor, partial(trace.save_context_payload, RUN_ATTEMPTED, summary)
            )
            with pytest.raises(RunCancelled, match="installation_unavailable"):
                task.result()
            assert await snapshot(factory) == before
        finally:
            await engine.dispose()

    asyncio.run(exercise())
