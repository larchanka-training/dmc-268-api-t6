"""Run creation, retry and rerun use durable publication settings (#212)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.findings_post_processor import FindingsPostProcessor
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.rerun_run import RerunOutcome, create_rerun
from app.modules.reviews.application.review_output import parse_review_output
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.models import Run
from app.modules.reviews.infrastructure.policy_snapshot_unit_of_work import (
    SqlAlchemyPolicySnapshotUnitOfWork,
)
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunStore
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyReviewOutputRepository
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from tests.portal_postgres import NOW, PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_prepare_policy_snapshot import Source
from tests.test_severity_threshold_postgres import RecordingQueue

_PATH = "app/charge.py"
_PATCH = "@@ -10,4 +10,5 @@\n context\n+added\n context\n context\n context"


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test publication pins")
    with portal_schema(url, None) as schema:
        yield schema


async def _settings(factory: async_sessionmaker[AsyncSession], threshold: str, cap: int) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE repositories SET severity_threshold=:threshold, "
                "max_comments=:cap WHERE id=:repo"
            ),
            {"threshold": threshold, "cap": cap, "repo": REPO_A},
        )


async def _ready(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": RUN_ATTEMPTED}
        )
        await session.execute(
            text("UPDATE code_changes SET head_sha=:head WHERE id=:pr"),
            {"head": "f" * 40, "pr": PR_NEW},
        )


async def _insert(session: AsyncSession) -> UUID:
    store = SqlAlchemyWebhookRunStore(session)
    candidate = await store.lock_candidate(PR_NEW)
    assert isinstance(candidate, RunInsertCandidate)
    message = await store.insert_webhook_run(candidate, NOW)
    assert isinstance(message, PendingRunMessage)
    return message.run_id


async def _claim(factory: async_sessionmaker[AsyncSession], run_id: UUID, attempt: int) -> None:
    async with factory.begin() as session:
        assert (
            await SqlAlchemyRunLifecycleStore(session).claim(
                run_id, worker_id="worker", now=NOW, lease_until=NOW + timedelta(minutes=10)
            )
            == attempt
        )
        await session.execute(
            text("""
            INSERT INTO code_change_diffs (
                id, run_id, code_change_id, head_sha, filename, patch, additions, deletions, changes
            ) VALUES (gen_random_uuid(), :run, :pr, :head, :path, :patch, 1, 0, 1)
            ON CONFLICT DO NOTHING
        """),
            {"run": run_id, "pr": PR_NEW, "head": "f" * 40, "path": _PATH, "patch": _PATCH},
        )


async def _publication(
    factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> tuple[object, ...]:
    async with factory() as session:
        context = await SqlAlchemyReviewOutputRepository(session).get_post_processing_input(run_id)
    assert context is not None
    output = parse_review_output(_answer())
    processed = FindingsPostProcessor.from_default_patterns().process(
        output,
        diff_map=context.diff_map,
        rule_names=context.rule_names,
        repository_max_inline=context.repository_max_inline,
        severity_threshold=context.severity_threshold,
        checked_paths=context.checked_paths,
    )
    return (
        context.severity_threshold,
        context.repository_max_inline,
        [item.finding.title for item in processed.inline],
        [item.finding.title for item in processed.body_only],
        [(item.finding.title, item.drop_reason) for item in processed.dropped],
    )


def _answer() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": _PATH,
                "line": line,
                "severity": severity,
                "category": "correctness",
                "title": title,
                "body": "A charge result can be lost during retries.",
                "confidence": 0.9,
                "start_line": None,
                "suggestion": None,
                "rule_name": None,
            }
            for line, severity, title in (
                (10, "critical", "Lost payment"),
                (11, "high", "Unchecked charge"),
                (12, "medium", "Missing retry guard"),
                (13, "low", "Loose comparison"),
                (14, "info", "Optional safeguard"),
            )
        ],
        "summary": {
            "problem": "Charge processing risks.",
            "done_well": "Small change.",
            "effort": "small",
        },
    }


@pytest.mark.integration
def test_webhook_creation_reads_fresh_settings_and_rolls_back_with_its_transaction(
    env: Env,
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            # Keep an obsolete Repository object alive in the insertion session.
            async with factory() as session:
                repository = await session.get(Repository, REPO_A)
                assert repository is not None and repository.max_comments == 10
                await _settings(factory, "warning", 1)
                run_id = await _insert(session)
                row = await session.get(Run, run_id)
                assert row is not None
                assert (row.severity_threshold, row.max_comments) == ("warning", 1)
                await session.rollback()
            async with factory() as session:
                assert await session.get(Run, run_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_rerun_reads_current_settings_from_a_fresh_repository_pair(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            async with factory() as session:
                repository = await session.get(Repository, REPO_A)
                assert repository is not None and repository.max_comments == 10
                await _settings(factory, "critical", 3)
                result = await create_rerun(
                    SqlAlchemyRerunStore(session, allow_unscoped=True), RUN_ATTEMPTED, NOW
                )
                assert result.outcome is RerunOutcome.CREATED
                assert result.message is not None
                row = await session.get(Run, result.message.run_id)
                assert row is not None
                assert (row.severity_threshold, row.max_comments) == ("critical", 3)
                original = await session.get(Run, RUN_ATTEMPTED)
                assert original is not None
                assert (original.severity_threshold, original.max_comments) == (None, None)
                await session.rollback()
            async with factory() as session:
                assert await session.get(Run, result.message.run_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("historical", [False, True])
def test_snapshot_prepared_after_settings_change_keeps_run_pair(env: Env, historical: bool) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _settings(factory, "warning", 1)
            if historical:
                run_id = RUN_ATTEMPTED
                async with factory.begin() as session:
                    await session.execute(
                        text("UPDATE runs SET state='queued' WHERE id=:run"), {"run": run_id}
                    )
            else:
                async with factory.begin() as session:
                    run_id = await _insert(session)
            await _claim(factory, run_id, 2 if historical else 1)
            await _settings(factory, "critical", 3)
            prepare = PreparePolicySnapshot(
                uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(factory),
                source_factory=lambda pins: Source(),
            )
            first = await prepare.execute(run_id)
            expected = (None, None) if historical else ("warning", 1)
            assert (first.severity_threshold, first.max_comments) == expected
            await _settings(factory, "all", 5)
            assert await prepare.execute(run_id) == first
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                pins = await work.policy.get_pins(run_id)
                assert pins is not None
                assert (pins.severity_threshold, pins.max_comments) == expected
                assert await work.policy.get_snapshot(run_id) == first
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_created_run_and_retry_keep_pins_but_rerun_uses_current_pair(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _settings(factory, "warning", 1)
            async with factory.begin() as session:
                run_id = await _insert(session)
            await _claim(factory, run_id, 1)
            await _settings(factory, "critical", 3)
            # No policy snapshot exists: the current legacy worker must honor Run pins.
            expected = (
                "warning",
                1,
                ["Lost payment"],
                ["Unchecked charge", "Missing retry guard", "Loose comparison"],
                [("Optional safeguard", "severity_threshold")],
            )
            assert await _publication(factory, run_id) == expected
            async with factory.begin() as session:
                assert await SqlAlchemyRunLifecycleStore(session).requeue(
                    run_id, worker_id="worker", available_at=NOW
                )
            await _settings(factory, "all", 2)
            await _claim(factory, run_id, 2)
            assert await _publication(factory, run_id) == expected
            queue = RecordingQueue()
            assert await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(run_id, _answer())
            assert len(queue.pointers) == 1 and queue.pointers[0].run_id == run_id
            async with factory() as session:
                findings = (
                    await session.execute(
                        text(
                            "SELECT title, inline_comment, drop_reason FROM findings "
                            "WHERE run_id=:run ORDER BY line_start"
                        ),
                        {"run": run_id},
                    )
                ).all()
                assert [tuple(row) for row in findings] == [
                    ("Lost payment", True, None),
                    ("Unchecked charge", False, None),
                    ("Missing retry guard", False, None),
                    ("Loose comparison", False, None),
                    ("Optional safeguard", False, "severity_threshold"),
                ]
                action = (
                    await session.execute(
                        text(
                            "SELECT request, response FROM run_actions "
                            "WHERE run_id=:run AND tool='review.postprocess'"
                        ),
                        {"run": run_id},
                    )
                ).one()
                assert action.request["severity_threshold"] == "warning"
                assert action.request["max_inline"] == 1
                assert action.response["inline"] == 1 and action.response["body_only"] == 3
                assert action.response["dropped"] == [
                    {"index": 4, "drop_reason": "severity_threshold"}
                ]
            async with factory.begin() as session:
                assert await SqlAlchemyRunLifecycleStore(session).finish(
                    run_id,
                    from_state="publishing",
                    worker_id="worker",
                    state="succeeded",
                    error_code=None,
                    error_message=None,
                    now=NOW,
                )
            async with factory.begin() as session:
                result = await create_rerun(
                    SqlAlchemyRerunStore(session, allow_unscoped=True), run_id, NOW
                )
                assert result.outcome is RerunOutcome.CREATED and result.message is not None
                rerun_id = result.message.run_id
            await _claim(factory, rerun_id, 1)
            assert await _publication(factory, rerun_id) == (
                "all",
                2,
                ["Lost payment", "Unchecked charge"],
                ["Missing retry guard", "Loose comparison", "Optional safeguard"],
                [],
            )
            async with factory() as session:
                original = await session.get(Run, run_id)
                assert original is not None and original.attempt == 2
                assert (original.severity_threshold, original.max_comments) == ("warning", 1)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_historical_null_run_keeps_live_fallback_even_after_snapshot_and_retry(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _claim(factory, RUN_ATTEMPTED, 2)
            prepare = PreparePolicySnapshot(
                uow_factory=lambda: SqlAlchemyPolicySnapshotUnitOfWork(factory),
                source_factory=lambda pins: Source(),
            )
            snapshot = await prepare.execute(RUN_ATTEMPTED)
            assert (snapshot.severity_threshold, snapshot.max_comments) == (None, None)
            await _settings(factory, "critical", 3)
            assert await _publication(factory, RUN_ATTEMPTED) == (
                "critical",
                3,
                ["Lost payment", "Unchecked charge"],
                [],
                [
                    ("Missing retry guard", "severity_threshold"),
                    ("Loose comparison", "severity_threshold"),
                    ("Optional safeguard", "severity_threshold"),
                ],
            )
            async with factory.begin() as session:
                assert await SqlAlchemyRunLifecycleStore(session).requeue(
                    RUN_ATTEMPTED, worker_id="worker", available_at=NOW
                )
            await _settings(factory, "all", 1)
            await _claim(factory, RUN_ATTEMPTED, 3)
            assert await prepare.execute(RUN_ATTEMPTED) == snapshot
            assert await _publication(factory, RUN_ATTEMPTED) == (
                "all",
                1,
                ["Lost payment"],
                [
                    "Unchecked charge",
                    "Missing retry guard",
                    "Loose comparison",
                    "Optional safeguard",
                ],
                [],
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_snapshot_pins_roll_back_with_the_callers_unit_of_work(env: Env) -> None:
    from app.modules.reviews.application.prepare_policy_snapshot import build_policy_snapshot
    from app.modules.reviews.application.review_ignore import parse_review_ignore
    from tests.test_policy_snapshot_postgres import _documents

    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _ready(factory)
            await _settings(factory, "warning", 1)
            async with factory.begin() as session:
                run_id = await _insert(session)
            await _claim(factory, run_id, 1)
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                pins = await work.policy.get_pins(run_id)
                assert pins is not None
                rules, agents = _documents()
                await work.policy.stage_documents(run_id, (rules, agents))
                snapshot = build_policy_snapshot(
                    pins,
                    rules=rules.document,
                    agents=agents.document,
                    ignore=parse_review_ignore(rules.document.text),
                    snapshot_id=UUID(int=212),
                )
                stored = await work.policy.create_snapshot(snapshot)
                assert (stored.severity_threshold, stored.max_comments) == ("warning", 1)
                # No commit: the caller's exit must roll back snapshot and staged documents.
            async with SqlAlchemyPolicySnapshotUnitOfWork(factory) as work:
                assert await work.policy.get_snapshot(run_id) is None
                assert await work.policy.get_staged_documents(run_id) == ()
                pins = await work.policy.get_pins(run_id)
                assert pins is not None
                assert (pins.severity_threshold, pins.max_comments) == ("warning", 1)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
