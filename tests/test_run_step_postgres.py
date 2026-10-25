"""NOTIFY run_step when a traced run action commits (#97)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.run_step_listener import _event
from app.bootstrap.run_step_listener import listen_forever as listen_run_steps  # noqa: I001
from app.bootstrap.run_update_listener import listen_forever as listen_run_updates
from app.modules.reviews.application.conventions import CachedConventions, ConventionsFile
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import ReviewOutput, ReviewSummary
from app.modules.reviews.application.run_coverage import CoverageItem, CoverageStatus, RunCoverage
from app.modules.reviews.application.run_events import (
    InMemoryRunStepHub,
    InMemoryRunUpdateHub,
    RunStep,
    RunUpdated,
)
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import NOW, PROMPT, REPO_A, WS_A, Env, portal_schema
from tests.test_run_stream_postgres import insert_queued_run

STARTED = datetime(2026, 10, 12, 9, 0, 0, 123456, tzinfo=UTC)
STARTED_AT = "2026-10-12T09:00:00.123456Z"
RUN_ATTEMPT = 1


async def allow_run_steps(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
    """``insert_run_action`` requires ``runs.attempt >= 1`` (D-4)."""
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET attempt = GREATEST(attempt, 1) WHERE id = :id"),
            {"id": run_id},
        )


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


@pytest.mark.integration
def test_committed_trace_notifies_run_step_and_a_rollback_does_not(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
    conninfo = env.database_url.replace("postgresql+psycopg://", "postgresql://")

    async def scenario() -> tuple[str, list[dict[str, object]]]:
        run_id = await insert_queued_run(factory)
        await allow_run_steps(factory, run_id)
        connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
        await connection.execute("LISTEN run_step")
        try:
            async with SqlAlchemyRunTraceUnitOfWork(factory) as uow:
                await uow.trace.add_action(
                    run_id,
                    "vcs.fetch_diff",
                    {"secret": "rolled-back"},
                    {"diff": "gone"},
                    STARTED,
                    999,
                )
                await uow.rollback()
            await trace.record(
                run_id, "vcs.fetch_diff", {"secret": "leak"}, {"files": 1}, STARTED, 5
            )
            await trace.record(run_id, 'say "ok"', {"secret": "leak"}, {"diff": "body"}, STARTED, 6)
            await trace.record(run_id, "context.build", {"secret": "leak"}, None, STARTED, 7)
            await trace.record(
                run_id,
                "llm.call",
                {"secret": "leak", "operation": "review"},
                {"error": {"type": "TimeoutError", "message": "slow"}},
                STARTED,
                8,
            )
            await trace.record(
                run_id,
                "github.publish_review",
                {"secret": "leak"},
                {"github_review_id": 9},
                STARTED,
                9,
            )
            received: list[dict[str, object]] = []
            async with asyncio.timeout(2):
                async for notify in connection.notifies():
                    payload = json.loads(notify.payload)
                    if payload.get("run_id") != str(run_id):
                        continue
                    received.append(payload)
                    if len(received) == 5:
                        break
            return str(run_id), received
        finally:
            await connection.close()
            await engine.dispose()

    run_id, received = asyncio.run(scenario())
    assert received == [
        _step(run_id, "vcs.fetch_diff", 0, True, 5),
        _step(run_id, 'say "ok"', 1, True, 6),
        _step(run_id, "context.build", 2, True, 7),
        _step(run_id, "llm.call", 3, False, 8, llm_operation="review"),
        _step(run_id, "github.publish_review", 4, True, 9),
    ]
    assert all(payload["attempt"] == RUN_ATTEMPT for payload in received)


@pytest.mark.integration
def test_conventions_on_attempt_two_notifies_a_frame_and_verification_does_not(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
    conninfo = env.database_url.replace("postgresql+psycopg://", "postgresql://")

    async def scenario() -> tuple[str, list[dict[str, object]]]:
        run_id = await insert_queued_run(factory)
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE runs SET attempt = 2 WHERE id = :id"),
                {"id": run_id},
            )
        connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
        await connection.execute("LISTEN run_step")
        try:
            await trace.record(
                run_id,
                "llm.call",
                {"operation": "conventions"},
                {"files": []},
                STARTED,
                4,
            )
            await trace.record(
                run_id,
                "llm.call",
                {"operation": "verification"},
                {"ok": True},
                STARTED,
                5,
            )
            received: list[dict[str, object]] = []
            async with asyncio.timeout(2):
                async for notify in connection.notifies():
                    payload = json.loads(notify.payload)
                    if payload.get("run_id") != str(run_id):
                        continue
                    received.append(payload)
                    if len(received) == 2:
                        break
            return str(run_id), received
        finally:
            await connection.close()
            await engine.dispose()

    run_id, received = asyncio.run(scenario())
    assert received == [
        _step(run_id, "llm.call", 0, True, 4, attempt=2, llm_operation="conventions"),
        _step(run_id, "llm.call", 1, True, 5, attempt=2),
    ]
    assert "llm_operation" not in received[1]
    frames = [_event(json.dumps(payload)) for payload in received]
    assert frames[0] is not None and frames[0].llm_operation == "conventions"
    assert frames[0].attempt == 2
    assert frames[1] is not None and frames[1].llm_operation is None


@pytest.mark.integration
def test_step_listener_publishes_the_step_and_a_bad_payload_spares_run_updated(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
    step_hub = InMemoryRunStepHub()
    status_hub = InMemoryRunUpdateHub()
    database_url = f"{env.database_url}?options=-csearch_path%3D{env.schema}"

    async def scenario() -> tuple[UUID, RunStep, RunUpdated]:
        run_id = await insert_queued_run(factory)
        await allow_run_steps(factory, run_id)

        async def one_step(events: AsyncIterator[RunStep]) -> RunStep:
            async for event in events:
                if event.run_id == run_id:
                    return event
            raise AssertionError("step stream ended")

        async def one_status(events: AsyncIterator[RunUpdated]) -> RunUpdated:
            async for event in events:
                if event.run_id == run_id:
                    return event
            raise AssertionError("status stream ended")

        async with step_hub.subscribe() as steps, status_hub.subscribe() as updates:
            step_listener = asyncio.create_task(listen_run_steps(database_url, step_hub))
            status_listener = asyncio.create_task(listen_run_updates(database_url, status_hub))
            step_reader = asyncio.create_task(one_step(steps))
            status_reader = asyncio.create_task(one_status(updates))
            await asyncio.sleep(0.5)
            try:
                async with factory() as session, session.begin():
                    await session.execute(text("SELECT pg_notify('run_step', 'not-json')"))
                await trace.record(
                    run_id, "vcs.fetch_diff", {"secret": "leak"}, {"files": 1}, STARTED, 5
                )
                async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                    await uow.runs.claim(
                        run_id,
                        worker_id="w",
                        now=NOW,
                        lease_until=NOW + timedelta(minutes=5),
                    )
                    await uow.commit()
                step = await asyncio.wait_for(step_reader, timeout=2)
                status = await asyncio.wait_for(status_reader, timeout=2)
                return run_id, step, status
            finally:
                for task in (step_reader, status_reader, step_listener, status_listener):
                    task.cancel()
                await asyncio.gather(
                    step_reader,
                    status_reader,
                    step_listener,
                    status_listener,
                    return_exceptions=True,
                )
                await engine.dispose()

    caplog.set_level(logging.WARNING, logger="app.bootstrap.run_step_listener")
    run_id, step, status = asyncio.run(scenario())
    assert step == RunStep(run_id, "vcs.fetch_diff", 0, True, STARTED, 5, RUN_ATTEMPT)
    assert status == RunUpdated(run_id, "running")
    assert "Ignoring malformed run_step payload" in caplog.text
    assert "run_step listener lost its connection" not in caplog.text


@pytest.mark.integration
def test_publishing_transaction_notifies_review_steps_only_after_commit(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    conninfo = env.database_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = ReviewOutput(
        findings=[],
        summary=ReviewSummary(problem="A bug.", done_well="Small change.", effort="small"),
    )
    processed = ProcessedReviewOutput(
        inline=(),
        body_only=(),
        dropped=(),
        review_body="A bug.",
        coverage=RunCoverage(
            CoverageStatus.PARTIAL,
            0,
            reasons=(CoverageItem(None, "classification_unavailable"),),
        ),
    )

    async def scenario() -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
        run_id = await insert_queued_run(factory)
        await allow_run_steps(factory, run_id)
        connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
        await connection.execute("LISTEN run_step")
        await connection.execute("LISTEN run_updated")
        try:
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                stored = await uow.reviews.store_review_output(
                    run_id,
                    {
                        "findings": [],
                        "summary": {
                            "problem": "A bug.",
                            "done_well": "Small change.",
                            "effort": "small",
                        },
                    },
                    parsed,
                    processed,
                )
                assert stored is not None
                await uow.reviews.enter_publishing(
                    run_id,
                    lease_until=NOW + timedelta(minutes=5),
                    postprocess_request={"min_confidence": 0.5, "max_inline": 8},
                    postprocess_response={"inline": 0, "body_only": 0, "dropped": []},
                    started_at=STARTED,
                    duration_ms=11,
                )
                early_steps, early_updates = await _matching(
                    connection,
                    run_id,
                    want_steps=1,
                    want_updates=1,
                    seconds=0.3,
                    allow_timeout=True,
                )
                assert early_steps == []
                assert early_updates == []
                await uow.commit()
            steps, updates = await _matching(
                connection, run_id, want_steps=2, want_updates=1, seconds=2
            )
            return str(run_id), steps, updates
        finally:
            await connection.close()
            await engine.dispose()

    run_id, steps, updates = asyncio.run(scenario())
    assert steps[0]["run_id"] == run_id
    assert steps[0]["tool"] == "llm.review_output"
    assert steps[0]["index"] == 0
    assert steps[0]["ok"] is True
    assert steps[0]["duration_ms"] == 0
    started_at = steps[0]["started_at"]
    assert isinstance(started_at, str) and started_at.endswith("Z")
    assert steps[1] == _step(run_id, "review.postprocess", 1, True, 11)
    assert updates == [
        {"run_id": run_id, "workspace_id": str(WS_A), "status": "publishing"},
    ]


@pytest.mark.integration
def test_rolled_back_publishing_transaction_emits_neither_step_nor_status(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    conninfo = env.database_url.replace("postgresql+psycopg://", "postgresql://")

    async def scenario() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        run_id = await insert_queued_run(factory)
        await allow_run_steps(factory, run_id)
        connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
        await connection.execute("LISTEN run_step")
        await connection.execute("LISTEN run_updated")
        try:
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                stored = await uow.reviews.store_review_output(
                    run_id, _model_output(), _parsed_output(), _processed_output()
                )
                assert stored is not None
                await uow.reviews.enter_publishing(
                    run_id,
                    lease_until=NOW + timedelta(minutes=5),
                    postprocess_request={"min_confidence": 0.5, "max_inline": 8},
                    postprocess_response={"inline": 0, "body_only": 0, "dropped": []},
                    started_at=STARTED,
                    duration_ms=11,
                )
                await uow.rollback()
            return await _matching(
                connection,
                run_id,
                want_steps=1,
                want_updates=1,
                seconds=0.5,
                allow_timeout=True,
            )
        finally:
            await connection.close()
            await engine.dispose()

    steps, updates = asyncio.run(scenario())
    assert steps == []
    assert updates == []


@pytest.mark.integration
def test_saved_repository_conventions_notify_run_step_with_the_assigned_index(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
    conninfo = env.database_url.replace("postgresql+psycopg://", "postgresql://")

    async def scenario() -> tuple[str, list[dict[str, object]]]:
        run_id = await insert_queued_run(factory)
        await allow_run_steps(factory, run_id)
        connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
        await connection.execute("LISTEN run_step")
        try:
            await trace.record(run_id, "context.build", {}, {"files": 1}, STARTED, 5)
            prior, _updates = await _matching(connection, run_id, want_steps=1, want_updates=0)
            assert prior == [_step(str(run_id), "context.build", 0, True, 5)]
            async with SqlAlchemyRepositoryConventionsUnitOfWork(factory) as uow:
                await uow.conventions.save_and_record_trace(
                    run_id,
                    CachedConventions(
                        repository_id=REPO_A,
                        agents_md_sha=None,
                        prompt_version_id=PROMPT,
                        key_patterns=("One.", "Two.", "Three."),
                        recommendations=("A.", "B.", "C.", "D.", "E."),
                        languages={"python": 1},
                    ),
                    (ConventionsFile(path="src/app.py", relevance="Holds the bug."),),
                )
                early, _early_updates = await _matching(
                    connection,
                    run_id,
                    want_steps=1,
                    want_updates=0,
                    seconds=0.3,
                    allow_timeout=True,
                )
                assert early == []
                await uow.commit()
            steps, _committed_updates = await _matching(
                connection, run_id, want_steps=1, want_updates=0
            )
            return str(run_id), steps
        finally:
            await connection.close()
            await engine.dispose()

    run_id, steps = asyncio.run(scenario())
    assert steps[0]["run_id"] == run_id
    assert steps[0]["tool"] == "llm.repo_conventions"
    assert steps[0]["index"] == 1
    assert steps[0]["ok"] is True
    assert steps[0]["duration_ms"] == 0
    started_at = steps[0]["started_at"]
    assert isinstance(started_at, str) and started_at.endswith("Z")


def _parsed_output() -> ReviewOutput:
    return ReviewOutput(
        findings=[],
        summary=ReviewSummary(problem="A bug.", done_well="Small change.", effort="small"),
    )


def _processed_output() -> ProcessedReviewOutput:
    return ProcessedReviewOutput(
        inline=(),
        body_only=(),
        dropped=(),
        review_body="A bug.",
        coverage=RunCoverage(
            CoverageStatus.PARTIAL,
            0,
            reasons=(CoverageItem(None, "classification_unavailable"),),
        ),
    )


def _model_output() -> dict[str, object]:
    return {
        "findings": [],
        "summary": {"problem": "A bug.", "done_well": "Small change.", "effort": "small"},
    }


async def _matching(
    connection: psycopg.AsyncConnection,
    run_id: UUID,
    *,
    want_steps: int,
    want_updates: int,
    seconds: float = 2,
    allow_timeout: bool = False,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    steps: list[dict[str, object]] = []
    updates: list[dict[str, object]] = []
    try:
        async with asyncio.timeout(seconds):
            async for notify in connection.notifies():
                payload = json.loads(notify.payload)
                if not isinstance(payload, dict) or payload.get("run_id") != str(run_id):
                    continue
                if notify.channel == "run_step":
                    steps.append(payload)
                elif notify.channel == "run_updated":
                    updates.append(payload)
                if len(steps) >= want_steps and len(updates) >= want_updates:
                    break
    except TimeoutError:
        if not allow_timeout:
            raise
    return steps, updates


def _step(
    run_id: str,
    tool: str,
    index: int,
    ok: bool,
    duration_ms: int,
    *,
    attempt: int = RUN_ATTEMPT,
    llm_operation: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "tool": tool,
        "index": index,
        "ok": ok,
        "started_at": STARTED_AT,
        "duration_ms": duration_ms,
        "attempt": attempt,
    }
    if llm_operation is not None:
        payload["llm_operation"] = llm_operation
    return payload
