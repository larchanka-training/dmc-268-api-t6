"""Provider projections and delivered historical Runs retain original installation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.check_runs import (
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, HandleReviewRun
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestRunCanceller,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import (
    INSTALL_A,
    INSTALL_B,
    REPO_A,
    RUN_ATTEMPTED,
    WS_A,
    Env,
    portal_schema,
)
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
@pytest.mark.parametrize("origin", ["active", "retired", "rebound", "unknown"])
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
                assert guard.repository_enabled is (origin == "active")
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
                    assert after["state"] == "skipped" and after["error_code"] == "repo_disabled"
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
def test_claim_holds_source_and_repository_before_waiting_for_run_lock(guard_env: Env) -> None:
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
            await prepare(factory, "active", "queued")
            async with factory.begin() as blocker:
                await blocker.execute(
                    text("SELECT id FROM runs WHERE id=:id FOR UPDATE"), {"id": RUN_ATTEMPTED}
                )

                async def claim() -> bool:
                    async with factory.begin() as session:
                        guard = await SqlAlchemyRunLifecycleStore(session).lock_for_claim(
                            RUN_ATTEMPTED
                        )
                        assert guard is not None
                        return guard.repository_enabled

                task = asyncio.create_task(claim())
                await asyncio.wait_for(waiting.wait(), 5)
                assert not task.done()
                for table, identifier in (
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                ):
                    async with factory() as observer:
                        with pytest.raises(DBAPIError) as refused:
                            await observer.execute(
                                text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
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
                and "FOR UPDATE" in statement
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
                    assert tuple(old) == ("skipped", "repo_disabled", INSTALL_A)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            await engine.dispose()

    asyncio.run(exercise())
