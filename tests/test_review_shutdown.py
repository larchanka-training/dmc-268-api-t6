"""Cancellation through the public review handler and its persisted lease boundary (#185)."""

from __future__ import annotations

import asyncio
import gc
import os
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.check_runs import (
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
    ReviewPipeline,
)
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleUnitOfWork
from tests.portal_postgres import REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_app_reinstall_postgres import wait_for_blocked_backend
from tests.test_reconcile_runs import Publisher
from tests.test_review_run_worker import NOW, RUN, FakeRun, FakeStore, RetryQueue


@dataclass
class Database:
    """Transaction snapshots commit atomically; discarded snapshots model rollback."""

    row: FakeRun = field(default_factory=FakeRun)
    pause: str = ""
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    transactions: int = 0
    commits: int = 0
    cleanup_started: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_release: asyncio.Event = field(default_factory=asyncio.Event)
    pause_cleanup: bool = False
    fail_cleanup: bool = False

    def uow(self) -> Transaction:
        self.transactions += 1
        return Transaction(self, self.transactions)

    async def checkpoint(self, phase: str, transaction: int) -> None:
        if transaction == 1 and self.pause == phase:
            self.reached.set()
            await self.release.wait()


class Store(FakeStore):
    def __init__(self, database: Database, transaction: int) -> None:
        super().__init__(deepcopy(database.row))
        self.database = database
        self.transaction = transaction

    async def claim(
        self, run_id: UUID, *, worker_id: str, now: datetime, lease_until: datetime
    ) -> int:
        result = await super().claim(run_id, worker_id=worker_id, now=now, lease_until=lease_until)
        await self.database.checkpoint("claim", self.transaction)
        return result

    async def extend_lease(self, run_id: UUID, worker_id: str, lease_until: datetime) -> bool:
        if lease_until < NOW:
            self.database.cleanup_started.set()
            if self.database.pause_cleanup:
                await self.database.cleanup_release.wait()
            if self.database.fail_cleanup:
                raise RuntimeError("cleanup database unavailable")
        if self.run.state not in {"running", "publishing"} or self.run.worker_id != worker_id:
            return False
        return await super().extend_lease(run_id, worker_id, lease_until)


class Transaction:
    def __init__(self, database: Database, transaction: int) -> None:
        self.database = database
        self.transaction = transaction
        self.runs = Store(database, transaction)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            await self.database.checkpoint("exit", self.transaction)

    async def commit(self) -> None:
        await self.database.checkpoint("commit_before", self.transaction)
        self.database.row = deepcopy(self.runs.run)
        self.database.commits += 1
        await self.database.checkpoint("commit_after", self.transaction)

    async def rollback(self) -> None:
        pass


class PausedCheckRuns:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        await checkpoint()
        self.database.reached.set()
        await self.database.release.wait()


async def cancel_at_checkpoint(task: asyncio.Task[object], reached: asyncio.Event) -> None:
    async with asyncio.timeout(2):
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("stage", ["initial_check_run", "pipeline"])
def test_cancelled_claim_expires_only_lease_after_pipeline_quiescence(stage: str) -> None:
    asyncio.run(cancelled_claim(stage))


async def cancelled_claim(stage: str) -> None:
    database = Database()
    quiescent = asyncio.Event()
    retry = RetryQueue()

    async def pipeline(claimed: ClaimedAttempt) -> bool:
        database.reached.set()
        try:
            await database.release.wait()
        finally:
            quiescent.set()
        return True

    handler = HandleReviewRun(
        uow_factory=database.uow,
        pipeline=pipeline,
        retry_queue=retry,
        check_runs=PausedCheckRuns(database) if stage == "initial_check_run" else None,
        worker_id="worker-185",
        now=lambda: NOW,
    )
    await cancel_at_checkpoint(asyncio.create_task(handler.execute(RUN)), database.reached)

    assert stage == "initial_check_run" or quiescent.is_set()
    assert database.row.state == "running"
    assert database.row.worker_id == "worker-185"
    assert database.row.attempt == 1
    assert database.row.error_code is None
    assert database.row.error_message is None
    assert database.row.lease_until == NOW - timedelta(microseconds=1)
    assert database.commits == 2
    assert retry.published == []


def make_handler(database: Database, pipeline: ReviewPipeline) -> HandleReviewRun:
    return HandleReviewRun(
        uow_factory=database.uow,
        pipeline=pipeline,
        retry_queue=RetryQueue(),
        check_runs=None,
        worker_id="worker-185",
        now=lambda: NOW,
    )


@pytest.mark.parametrize("stage", ["claim", "commit_before", "commit_after", "exit"])
def test_cancel_during_claim_transaction_checks_persisted_claim(stage: str) -> None:
    async def scenario() -> None:
        database = Database(pause=stage)

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            pytest.fail("pipeline must not start during interrupted claim")

        await cancel_at_checkpoint(
            asyncio.create_task(make_handler(database, pipeline).execute(RUN)), database.reached
        )
        if stage in {"commit_after", "exit"}:
            assert database.row.state == "running"
            assert database.row.attempt == 1
            assert database.row.lease_until == NOW - timedelta(microseconds=1)
            assert database.commits == 2
        else:
            assert database.row == FakeRun()
            assert database.commits == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "owner"),
    [("publishing", "worker-185"), ("failed", "worker-185"), ("running", "new-worker")],
)
def test_cancel_preserves_publishing_terminal_and_replaced_owner(state: str, owner: str) -> None:
    async def scenario() -> None:
        database = Database()

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            database.reached.set()
            await database.release.wait()
            return True

        task = asyncio.create_task(make_handler(database, pipeline).execute(RUN))
        await database.reached.wait()
        database.row.state = state
        database.row.worker_id = owner
        database.row.error_code = "preserved"
        before = deepcopy(database.row)
        await cancel_at_checkpoint(task, database.reached)
        assert database.row == before
        assert database.commits == 1

    asyncio.run(scenario())


def test_cancel_waits_for_cleanup_commit_through_repeated_caller_cancel() -> None:
    async def scenario() -> None:
        database = Database(pause_cleanup=True)

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            database.reached.set()
            await database.release.wait()
            return True

        task = asyncio.create_task(make_handler(database, pipeline).execute(RUN))
        async with asyncio.timeout(2):
            await database.reached.wait()
            task.cancel("original cancellation")
            await database.cleanup_started.wait()
            task.cancel("second cancellation")
            # A ready-loop barrier ensures the repeated cancellation has been delivered.
            await asyncio.sleep(0)
            assert not task.done()
            assert database.row.lease_until == NOW + timedelta(minutes=5)
            database.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError, match="original cancellation"):
                await task
        assert database.commits == 2
        assert database.row.lease_until == NOW - timedelta(microseconds=1)

    asyncio.run(scenario())


def test_cleanup_failure_is_logged_and_preserves_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        database = Database(fail_cleanup=True)

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            database.reached.set()
            await database.release.wait()
            return True

        await cancel_at_checkpoint(
            asyncio.create_task(make_handler(database, pipeline).execute(RUN)), database.reached
        )
        assert database.row.lease_until == NOW + timedelta(minutes=5)
        assert database.commits == 1
        assert "interrupted lease cleanup failed" in caplog.text
        assert "cleanup database unavailable" in caplog.text

    asyncio.run(scenario())


def test_nonquiescent_pipeline_keeps_lease_and_late_failure_is_retrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        database = Database()
        child: asyncio.Task[bool] | None = None

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            nonlocal child
            child = asyncio.current_task()
            database.reached.set()
            try:
                await database.release.wait()
            except asyncio.CancelledError:
                await database.release.wait()
            raise RuntimeError("late pipeline failure")

        task = asyncio.create_task(make_handler(database, pipeline).execute(RUN))
        try:
            async with asyncio.timeout(7):
                await database.reached.wait()
                task.cancel()
                while "pipeline is still active; interrupted lease retained" not in caplog.text:
                    await asyncio.sleep(0.001)
                task.cancel("repeated caller cancellation")
                await asyncio.sleep(0)
                assert not task.done()
                assert child is not None and not child.done()
                assert child.cancelling() == 1
                assert database.row.lease_until == NOW + timedelta(minutes=5)
                assert database.commits == 1
                database.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert database.row.lease_until == NOW - timedelta(microseconds=1)
            assert database.commits == 2
        finally:
            database.release.set()
            await asyncio.gather(task, return_exceptions=True)
            if child is not None:
                await asyncio.gather(child, return_exceptions=True)
        assert "late pipeline failure" in caplog.text
        assert "cancelled pipeline failed during cleanup" in caplog.text

    asyncio.run(scenario())


@pytest.fixture
def shutdown_postgres() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def prepare_claim(factory: async_sessionmaker[AsyncSession], *, attempt: int = 0) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE repositories SET enabled=true,installation_available=true WHERE id=:id"),
            {"id": REPO_A},
        )
        await session.execute(
            text(
                "UPDATE runs SET state='queued',attempt=:attempt,available_at=:now,"
                "lease_until=null,worker_id=null,cancel_requested=false WHERE id=:id"
            ),
            {"id": RUN_ATTEMPTED, "attempt": attempt, "now": NOW},
        )


async def lease_row(session: AsyncSession) -> dict[str, object]:
    result = await session.execute(
        text(
            "SELECT state,attempt,lease_until,worker_id,error_code,error_message "
            "FROM runs WHERE id=:id"
        ),
        {"id": RUN_ATTEMPTED},
    )
    return dict(result.mappings().one())


@pytest.mark.integration
@pytest.mark.parametrize("transition", ["owned_running", "publishing", "failed", "new_owner"])
def test_postgres_cleanup_serializes_with_state_and_owner_change(
    shutdown_postgres: Env, transition: str
) -> None:
    async def scenario() -> None:
        engine = shutdown_postgres.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        started = asyncio.Event()
        quiescent = asyncio.Event()

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                quiescent.set()
            return True

        handler = HandleReviewRun(
            uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
            pipeline=pipeline,
            retry_queue=RetryQueue(),
            check_runs=None,
            worker_id="worker-185",
            now=lambda: NOW,
        )
        task: asyncio.Task[DeliveryOutcome] | None = None
        try:
            await prepare_claim(factory)
            task = asyncio.create_task(handler.execute(RUN_ATTEMPTED))
            async with asyncio.timeout(5):
                await started.wait()
            async with factory() as blocker, factory() as observer:
                async with blocker.begin():
                    await blocker.execute(
                        text("SELECT id FROM runs WHERE id=:id FOR UPDATE"),
                        {"id": RUN_ATTEMPTED},
                    )
                    blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                    assert isinstance(blocker_pid, int)
                    if transition in {"publishing", "failed"}:
                        await blocker.execute(
                            text("UPDATE runs SET state=:state WHERE id=:id"),
                            {"state": transition, "id": RUN_ATTEMPTED},
                        )
                    elif transition == "new_owner":
                        await blocker.execute(
                            text("UPDATE runs SET worker_id='replacement' WHERE id=:id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    before = await lease_row(blocker)
                    task.cancel()
                    await wait_for_blocked_backend(observer, blocker_pid, task)
                    assert quiescent.is_set()
                    assert not task.done()
                async with asyncio.timeout(5):
                    with pytest.raises(asyncio.CancelledError):
                        await task
            async with factory() as session:
                after = await lease_row(session)
            if transition == "owned_running":
                assert after == {**before, "lease_until": NOW - timedelta(microseconds=1)}
            else:
                assert after == before
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_postgres_interrupted_third_attempt_uses_existing_t13_without_fourth_claim(
    shutdown_postgres: Env,
) -> None:
    async def scenario() -> None:
        engine = shutdown_postgres.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        started = asyncio.Event()
        attempts: list[int] = []
        publisher = Publisher()

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            attempts.append(claimed.attempt)
            started.set()
            await asyncio.Event().wait()
            return True

        handler = HandleReviewRun(
            uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
            pipeline=pipeline,
            retry_queue=RetryQueue(),
            check_runs=None,
            worker_id="worker-185",
            now=lambda: NOW,
        )
        try:
            await prepare_claim(factory, attempt=2)
            await cancel_at_checkpoint(asyncio.create_task(handler.execute(RUN_ATTEMPTED)), started)
            async with factory() as session:
                interrupted = await lease_row(session)
            assert interrupted["state"] == "running"
            assert interrupted["attempt"] == 3
            assert interrupted["lease_until"] == NOW - timedelta(microseconds=1)
            assert (
                await ReconcileRuns(
                    uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                    run_publisher=publisher,
                    review_queue=publisher,
                    now=lambda: NOW,
                ).execute()
                == 1
            )
            assert len(publisher.runs) == 1
            assert publisher.runs[0][0] == RUN_ATTEMPTED
            # T13's informational transport attempt is unchanged; no fourth DB claim occurs.
            assert await handler.execute(RUN_ATTEMPTED) == DeliveryOutcome.ACK
            async with factory() as session:
                finished = await lease_row(session)
            assert finished["state"] == "failed"
            assert finished["error_code"] == "lease_expired"
            assert finished["attempt"] == 3
            assert finished["lease_until"] is None
            assert attempts == [3]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completed_pipeline_failure_is_retrieved_when_caller_cancel_wins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        database = Database()
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        parent: asyncio.Task[DeliveryOutcome] | None = None

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            assert parent is not None
            # Queue cancellation before asyncio.wait can resume and retrieve this result.
            loop.call_soon(parent.cancel)
            raise RuntimeError("pipeline failed on cancellation tie")

        try:
            parent = asyncio.create_task(make_handler(database, pipeline).execute(RUN))
            with pytest.raises(asyncio.CancelledError):
                await parent
            parent = None
            gc.collect()
            await asyncio.sleep(0)
            assert unhandled == []
            assert "pipeline failed on cancellation tie" in caplog.text
            assert "cancelled pipeline failed during cleanup" in caplog.text
            assert database.row.lease_until == NOW - timedelta(microseconds=1)
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())
