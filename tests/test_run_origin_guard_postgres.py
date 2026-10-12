"""Provider projections and delivered historical Runs retain original installation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application import handle_review_run
from app.modules.reviews.application.check_runs import (
    CheckRunGateway,
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.handle_review_run import (
    CheckRunReportUnitOfWork,
    ClaimedAttempt,
    HandleReviewRun,
)
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestRunCanceller,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import (
    HEAD,
    INSTALL_A,
    INSTALL_B,
    PR_NEW,
    REPO_A,
    RUN_ATTEMPTED,
    RUN_B,
    RUN_DONE,
    WS_A,
    Env,
    portal_schema,
)
from tests.test_reconcile_runs import Publisher
from tests.test_review_run_worker import RetryQueue, Selection


@pytest.fixture
def guard_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def prepare(factory: async_sessionmaker[AsyncSession], origin: str, state: str) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE runs SET origin_provider_installation_id=:origin,state=:state,"
                "available_at=clock_timestamp()-interval '1 minute',"
                "lease_until=clock_timestamp()-interval '1 minute',"
                "cancellation_signal_requested_at=clock_timestamp() WHERE id=:id"
            ),
            {
                "origin": None if origin == "unknown" else INSTALL_A,
                "state": state,
                "id": RUN_ATTEMPTED,
            },
        )
        await session.execute(
            text("UPDATE repositories SET enabled=true,installation_available=true WHERE id=:id"),
            {"id": REPO_A},
        )
        if origin == "retired":
            await session.execute(
                text("UPDATE provider_installations SET deleted_at=clock_timestamp() WHERE id=:id"),
                {"id": INSTALL_A},
            )
        elif origin == "repo_removed":
            await session.execute(
                text("UPDATE repositories SET installation_available=false WHERE id=:id"),
                {"id": REPO_A},
            )
        elif origin == "rebound":
            await session.execute(
                text("UPDATE provider_installations SET external_id=29 WHERE id=:id"),
                {"id": INSTALL_B},
            )
            await session.execute(
                text("UPDATE repositories SET provider_installation_id=:origin WHERE id=:id"),
                {"origin": INSTALL_B, "id": REPO_A},
            )


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["active", "retired", "rebound", "unknown", "repo_removed"])
@pytest.mark.parametrize("state", ["queued", "publishing", "cancelled"])
def test_provider_projections_never_reconstruct_another_or_unknown_installation(
    guard_env: Env,
    origin: str,
    state: str,
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, origin, state)
            async with factory() as session:
                store = SqlAlchemyRunLifecycleStore(session)
                guard = await store.lock_for_claim(RUN_ATTEMPTED)
                assert guard is not None
                assert guard.repository_enabled is True
                assert guard.origin_active is (origin == "active")
                reason = await store.cancellation_reason(RUN_ATTEMPTED)
                assert reason == (None if origin == "active" else "installation_unavailable")
                message = await store.run_message(RUN_ATTEMPTED)
                report = await store.check_run_report(RUN_ATTEMPTED)
                vcs = await SqlAlchemyRunRepository(
                    session=session, allow_unscoped=True
                ).get_run_vcs_input(RUN_ATTEMPTED)
                queued = await store.stale_queued(datetime.now(UTC) + timedelta(minutes=1), 100)
                publishing = await store.expired_publishing(datetime.now(UTC), 100)
                signals = await SqlAlchemyPullRequestRunCanceller(
                    session
                ).pending_cancellation_signals(100)
                if origin == "active":
                    assert message is not None and message.installation_id == 17
                    assert report is not None and report.target.installation_id == 17
                    assert vcs is not None and vcs.locator.installation_external_id == 17
                    assert bool(queued) is (state == "queued")
                    assert bool(publishing) is (state == "publishing")
                    assert bool(signals) is (state == "cancelled")
                    if signals:
                        assert signals[0].installation_id == 17
                else:
                    assert message is None and report is None and vcs is None
                    assert queued == ()
                    assert publishing == ()
                    assert signals == ()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["retired", "rebound", "unknown"])
@pytest.mark.parametrize("state", ["queued", "succeeded"])
def test_old_delivery_terminalizes_queue_and_preserves_terminal_history_without_provider_calls(
    guard_env: Env,
    origin: str,
    state: str,
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        calls: list[str] = []

        class Checks:
            async def upsert(
                self,
                target: CheckRunTarget,
                view: CheckRunView,
                *,
                checkpoint: RunProviderCheckpoint,
            ) -> None:
                await checkpoint()
                calls.append("check")

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            calls.append("pipeline")
            return False

        try:
            await prepare(factory, origin, state)
            async with factory() as session:
                before = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            worker = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                pipeline=pipeline,
                retry_queue=RetryQueue(),
                check_runs=Checks(),
                worker_id="new-worker",
                selection=Selection(),
            )
            assert await worker.execute(RUN_ATTEMPTED) == "ack"
            assert calls == []
            async with factory() as session:
                after = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
                if state == "succeeded":
                    assert after == before
                else:
                    assert (
                        after["state"] == "cancelled"
                        and after["error_code"] == "installation_unavailable"
                    )
                # Portal authorization stays on the current Repository grants.
                if origin != "rebound":
                    assert (
                        await SqlAlchemyRunRepository(
                            session=session, scope=AuthScope(42, (WS_A,))
                        ).get_run(RUN_ATTEMPTED)
                        is not None
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("fence", ["claim", "snapshot"])
def test_claim_holds_source_and_repository_before_waiting_for_run_lock(
    guard_env: Env, fence: str
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        waiting = asyncio.Event()

        def observe(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            if statement.startswith("SELECT runs.id") and "FOR UPDATE OF runs" in statement:
                waiting.set()

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            await prepare(factory, "active", "queued" if fence == "claim" else "running")
            async with factory.begin() as blocker:
                await blocker.execute(
                    text("SELECT id FROM runs WHERE id=:id FOR UPDATE"), {"id": RUN_ATTEMPTED}
                )

                async def claim() -> bool:
                    async with factory.begin() as session:
                        if fence == "snapshot":
                            await SqlAlchemyRunRepository(
                                session=session, allow_unscoped=True
                            ).store_diff_snapshots(
                                RUN_ATTEMPTED,
                                PR_NEW,
                                HEAD,
                                [DiffSnapshot(filename="fresh.py", patch="fresh")],
                            )
                            return True
                        guard = await SqlAlchemyRunLifecycleStore(session).lock_for_claim(
                            RUN_ATTEMPTED
                        )
                        assert guard is not None
                        return guard.repository_enabled

                task = asyncio.create_task(claim())
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                # A second worker may read the same provider rows while the first
                # waits for its distinct Run; FK inserts must retain KEY SHARE access.
                async with factory.begin() as reader:
                    await reader.execute(text("SET LOCAL lock_timeout='250ms'"))
                    other = await SqlAlchemyRunLifecycleStore(reader).lock_for_claim(RUN_DONE)
                    assert other is not None and other.origin_active
                    await reader.execute(
                        text(
                            "INSERT INTO runs (id,code_change_id,base_sha,base_ref,head_sha,state,"
                            "trigger,idempotency_key,engine,rule_version_id,prompt_version_id,"
                            "available_at,origin_provider_installation_id) "
                            "SELECT :fresh,code_change_id,base_sha,base_ref,head_sha,'succeeded',"
                            "'manual',:key,engine,rule_version_id,prompt_version_id,"
                            "clock_timestamp(),:origin FROM runs WHERE id=:old"
                        ),
                        {
                            "fresh": UUID(int=11331),
                            "key": "d" * 64,
                            "origin": INSTALL_A,
                            "old": RUN_ATTEMPTED,
                        },
                    )
                for table, identifier in (
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                ):
                    async with factory() as observer:
                        with pytest.raises(DBAPIError) as refused:
                            await observer.execute(
                                text(
                                    f"SELECT id FROM {table} WHERE id=:id FOR NO KEY UPDATE NOWAIT"
                                ),
                                {"id": identifier},
                            )
                        assert getattr(refused.value.orig, "sqlstate", None) == "55P03"
            assert await asyncio.wait_for(task, 5) is True
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("replacement", [False, True])
def test_source_replacement_while_claim_waits_refuses_old_work_but_new_origin_starts(
    guard_env: Env,
    replacement: bool,
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        waiting = asyncio.Event()
        calls: list[int | str] = []

        class Checks:
            async def upsert(
                self,
                target: CheckRunTarget,
                view: CheckRunView,
                *,
                checkpoint: RunProviderCheckpoint,
            ) -> None:
                await checkpoint()
                calls.append(target.installation_id)

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            calls.append("pipeline")
            return True

        def observe(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            if (
                statement.startswith("SELECT provider_installations.id")
                and "FOR SHARE" in statement
            ):
                waiting.set()

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            await prepare(factory, "active", "queued")
            async with factory() as session:
                terminal_before = (
                    (
                        await session.execute(
                            text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    )
                    .tuples()
                    .all()
                )
            worker = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                pipeline=pipeline,
                retry_queue=RetryQueue(),
                check_runs=Checks(),
                worker_id="new-worker",
                selection=Selection(),
            )
            async with factory.begin() as source:
                await source.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE"),
                    {"id": INSTALL_A},
                )
                task = asyncio.create_task(worker.execute(RUN_ATTEMPTED))
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                if replacement:
                    await source.execute(
                        text(
                            "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                            "WHERE id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
                    await source.execute(
                        text("UPDATE provider_installations SET external_id=29 WHERE id=:id"),
                        {"id": INSTALL_B},
                    )
                    await source.execute(
                        text(
                            "UPDATE repositories SET provider_installation_id=:origin WHERE id=:id"
                        ),
                        {"origin": INSTALL_B, "id": REPO_A},
                    )
            assert await asyncio.wait_for(task, 5) == "ack"
            assert calls == ([] if replacement else [17, "pipeline"])
            async with factory() as session:
                terminal_after = (
                    (
                        await session.execute(
                            text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    )
                    .tuples()
                    .all()
                )
                assert terminal_after == terminal_before
            if replacement:
                # Seed a distinct target Run with the explicit origin used by both writers.
                fresh_run = UUID(int=11329)
                async with factory.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO runs (id,code_change_id,base_sha,base_ref,head_sha,state,"
                            "trigger,idempotency_key,engine,rule_version_id,prompt_version_id,"
                            "available_at,origin_provider_installation_id) "
                            "SELECT :fresh,code_change_id,base_sha,base_ref,head_sha,'queued',"
                            "'manual',:key,engine,rule_version_id,prompt_version_id,"
                            "clock_timestamp(),:origin FROM runs WHERE id=:old"
                        ),
                        {
                            "origin": INSTALL_B,
                            "old": RUN_ATTEMPTED,
                            "fresh": fresh_run,
                            "key": "f" * 64,
                        },
                    )
                assert await worker.execute(fresh_run) == "ack"
                assert calls == [29, "pipeline"]
                async with factory() as session:
                    old = (
                        await session.execute(
                            text(
                                "SELECT state::text,error_code,origin_provider_installation_id "
                                "FROM runs WHERE id=:id"
                            ),
                            {"id": RUN_ATTEMPTED},
                        )
                    ).one()
                    assert tuple(old) == ("cancelled", "installation_unavailable", INSTALL_A)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["retired", "rebound", "unknown", "repo_removed"])
@pytest.mark.parametrize("state", ["queued", "running", "publishing"])
def test_reconciliation_terminalizes_stale_inactive_origins_without_queue_messages(
    guard_env: Env, origin: str, state: str
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, origin, state)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET available_at=clock_timestamp()-interval '20 minutes' "
                        "WHERE id=:id"
                    ),
                    {"id": RUN_ATTEMPTED},
                )
            publisher = Publisher()
            reconciler = ReconcileRuns(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                run_publisher=publisher,
                review_queue=publisher,
            )
            assert await reconciler.execute() == 0
            assert await reconciler.execute() == 0
            assert publisher.runs == [] and publisher.reviews == []
            async with factory() as session:
                result = (
                    await session.execute(
                        text("SELECT state::text,error_code,lease_until FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
                assert tuple(result) == ("cancelled", "installation_unavailable", None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["retired", "rebound", "unknown", "repo_removed"])
@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("failure_code", ["diff_fetch_failed", "llm_invalid_output"])
def test_retryable_worker_failure_after_retirement_terminalizes_without_retry(
    guard_env: Env, origin: str, attempt: int, failure_code: str
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        queue = RetryQueue()

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            await prepare(factory, origin, "running")
            raise RunFailure(failure_code, "provider failure after retirement")

        try:
            await prepare(factory, "active", "queued")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET attempt=:attempt WHERE id=:id"),
                    {"id": RUN_ATTEMPTED, "attempt": attempt},
                )
            worker = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                pipeline=pipeline,
                retry_queue=queue,
                check_runs=None,
                worker_id="owner",
            )
            assert await worker.execute(RUN_ATTEMPTED) == "ack"
            assert queue.published == []
            async with factory() as session:
                result = (
                    await session.execute(
                        text("SELECT state::text,error_code,lease_until FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
                assert tuple(result) == ("cancelled", "installation_unavailable", None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("state", ["queued", "running", "publishing", "succeeded"])
def test_reconciliation_preserves_unexpired_or_terminal_retired_work(
    guard_env: Env, state: str
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "retired", state)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET available_at=clock_timestamp()+interval '1 hour', "
                        "lease_until=clock_timestamp()+interval '1 hour' WHERE id=:id"
                    ),
                    {"id": RUN_ATTEMPTED},
                )
                before = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            publisher = Publisher()
            assert (
                await ReconcileRuns(
                    uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                    run_publisher=publisher,
                    review_queue=publisher,
                ).execute()
                == 0
            )
            async with factory() as session:
                after = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            assert after == before
            assert publisher.runs == [] and publisher.reviews == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("transition", ["requeue", "finish"])
def test_retired_attempt_cannot_be_terminalized_by_a_displaced_worker(
    guard_env: Env, transition: str
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "retired", "running")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET worker_id='current' WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            async with factory.begin() as session:
                store = SqlAlchemyRunLifecycleStore(session)
                if transition == "requeue":
                    changed = await store.requeue(
                        RUN_ATTEMPTED, worker_id="displaced", available_at=datetime.now(UTC)
                    )
                else:
                    changed = await store.finish(
                        RUN_ATTEMPTED,
                        from_state="running",
                        worker_id="displaced",
                        state="failed",
                        error_code="diff_fetch_failed",
                        error_message=None,
                        now=datetime.now(UTC),
                    )
                assert changed is False
            async with factory() as session:
                result = (
                    await session.execute(
                        text("SELECT state::text,error_code,worker_id FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
            assert tuple(result) == ("running", None, "current")
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_retired_retry_cancellation_finishes_now_instead_of_future_retry_time(
    guard_env: Env,
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "retired", "running")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET worker_id='owner' WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            before = datetime.now(UTC)
            async with factory.begin() as session:
                changed = await SqlAlchemyRunLifecycleStore(session).requeue(
                    RUN_ATTEMPTED, worker_id="owner", available_at=before + timedelta(minutes=10)
                )
                assert changed is False
            after = datetime.now(UTC)
            async with factory() as session:
                result = (
                    await session.execute(
                        text("SELECT state::text,error_code,finished_at FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
            assert tuple(result[:2]) == ("cancelled", "installation_unavailable")
            assert before <= result.finished_at <= after
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_reconciler_releases_first_origin_before_waiting_for_next_origin(guard_env: Env) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        waiting = asyncio.Event()

        def observe(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            if (
                statement.startswith("SELECT provider_installations.id")
                and "FOR SHARE" in statement
                and isinstance(parameters, dict)
                and RUN_B in parameters.values()
            ):
                waiting.set()

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            await prepare(factory, "retired", "running")
            async with factory.begin() as session:
                # Account callback order is B then A; Run discovery is A then B.
                await session.execute(
                    text("UPDATE provider_installations SET external_id=29 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("UPDATE provider_installations SET deleted_at=now() WHERE id=:id"),
                    {"id": INSTALL_B},
                )
                await session.execute(
                    text(
                        "UPDATE runs SET state='running',lease_until=now()-"
                        "interval '1 minute' WHERE id=:id"
                    ),
                    {"id": RUN_B},
                )
            publisher = Publisher()
            reconciler = ReconcileRuns(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                run_publisher=publisher,
                review_queue=publisher,
            )
            async with factory.begin() as callback:
                await callback.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE"),
                    {"id": INSTALL_B},
                )
                task = asyncio.create_task(reconciler.execute())
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                # A batch retaining A's share lock here would deadlock with callback.
                await callback.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": INSTALL_A},
                )
            assert await asyncio.wait_for(task, 5) == 0
            assert publisher.runs == [] and publisher.reviews == []
            async with factory() as session:
                result = (
                    await session.execute(
                        text(
                            "SELECT state::text,error_code FROM runs "
                            "WHERE id IN (:a,:b) ORDER BY id"
                        ),
                        {"a": RUN_ATTEMPTED, "b": RUN_B},
                    )
                ).all()
            assert [tuple(row) for row in result] == [
                ("cancelled", "installation_unavailable"),
                ("cancelled", "installation_unavailable"),
            ]
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["active", "retired", "rebound", "unknown", "repo_removed"])
@pytest.mark.parametrize("state", ["running", "publishing"])
def test_owning_worker_failure_terminalizes_inactive_origin_and_preserves_history(
    guard_env: Env, origin: str, state: str
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, origin, state)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET worker_id='owner',"
                        "lease_until=clock_timestamp()+interval '1 hour' WHERE id=:id"
                    ),
                    {"id": RUN_ATTEMPTED},
                )
                history_before = (
                    (
                        await session.execute(
                            text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    )
                    .tuples()
                    .all()
                )
            now = datetime.now(UTC)
            async with factory.begin() as session:
                assert (
                    await SqlAlchemyRunLifecycleStore(session).finish(
                        RUN_ATTEMPTED,
                        from_state=state,
                        worker_id="owner",
                        state="failed",
                        error_code="diff_fetch_failed",
                        error_message="provider failure",
                        now=now,
                    )
                    is True
                )
            async with factory() as session:
                result = (
                    await session.execute(
                        text(
                            "SELECT state::text,error_code,error_message,lease_until,finished_at,"
                            "worker_id,origin_provider_installation_id FROM runs WHERE id=:id"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
                if origin == "active":
                    assert tuple(result[:3]) == ("failed", "diff_fetch_failed", "provider failure")
                else:
                    assert tuple(result[:3]) == ("cancelled", "installation_unavailable", None)
                assert tuple(result[3:6]) == (None, now, "owner")
                assert result.origin_provider_installation_id == (
                    None if origin == "unknown" else INSTALL_A
                )
                finished_before = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
                history_after = (
                    (
                        await session.execute(
                            text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    )
                    .tuples()
                    .all()
                )
            assert history_after == history_before
            async with factory.begin() as session:
                assert (
                    await SqlAlchemyRunLifecycleStore(session).finish(
                        RUN_ATTEMPTED,
                        from_state=state,
                        worker_id="owner",
                        state="failed",
                        error_code="llm_invalid_output",
                        error_message="late failure",
                        now=now + timedelta(minutes=1),
                    )
                    is False
                )
            async with factory() as session:
                finished_after = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            assert finished_after == finished_before
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("state", ["queued", "running", "publishing"])
def test_retire_stale_origin_preserves_work_when_origin_is_active_again(
    guard_env: Env, state: str
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "repo_removed", state)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET installation_available=true WHERE id=:id"),
                    {"id": REPO_A},
                )
                before = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            async with factory.begin() as session:
                assert (
                    await SqlAlchemyRunLifecycleStore(session).retire_stale_origin(
                        RUN_ATTEMPTED, datetime.now(UTC), datetime.now(UTC)
                    )
                    is False
                )
            async with factory() as session:
                after = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            assert after == before
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["active", "retired", "rebound", "unknown", "repo_removed"])
def test_inactive_origin_cancellation_takes_priority_over_user_pause(
    guard_env: Env, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        engine = guard_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []
        check_updates: list[UUID] = []
        queue = RetryQueue()
        original_update = handle_review_run.update_check_run

        async def record_update(
            uow_factory: Callable[[], CheckRunReportUnitOfWork],
            gateway: CheckRunGateway | None,
            run_id: UUID,
            run_url: Callable[[UUID], str | None],
        ) -> None:
            check_updates.append(run_id)
            await original_update(uow_factory, gateway, run_id, run_url)

        monkeypatch.setattr(handle_review_run, "update_check_run", record_update)

        class Checks:
            async def upsert(
                self,
                target: CheckRunTarget,
                view: CheckRunView,
                *,
                checkpoint: RunProviderCheckpoint,
            ) -> None:
                calls.append("check")
                await checkpoint()

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            calls.append("pipeline")
            return False

        try:
            await prepare(factory, origin, "queued")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET enabled=false WHERE id=:id"), {"id": REPO_A}
                )
            assert (
                await HandleReviewRun(
                    uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                    pipeline=pipeline,
                    retry_queue=queue,
                    check_runs=Checks(),
                    worker_id="owner",
                ).execute(RUN_ATTEMPTED)
                == "ack"
            )
            assert calls == [] and queue.published == []
            async with factory() as session:
                result = (
                    await session.execute(
                        text(
                            "SELECT state::text,error_code,attempt,lease_until "
                            "FROM runs WHERE id=:id"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
            if origin == "active":
                assert tuple(result) == ("skipped", "repo_disabled", 1, None)
                assert check_updates == []
            else:
                assert tuple(result) == ("cancelled", "installation_unavailable", 1, None)
                assert check_updates == [RUN_ATTEMPTED]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
