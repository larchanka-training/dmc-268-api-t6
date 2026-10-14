"""RunGuard selection by the repository branch filter on PostgreSQL (#149, SD §6.1, PS T5)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.branch_selection_rule import (
    SqlAlchemyBranchSelectionRule,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleUnitOfWork
from app.worker import WorkerSettings, compose_worker_process
from tests.portal_postgres import PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema

_FILTER = ["main", "release/*"]
_REASON = "Целевая ветка PR не подходит под фильтр веток репозитория."


@pytest.fixture
def selection_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


async def _prepare(
    factory: async_sessionmaker[AsyncSession],
    *,
    branch_filter: list[str],
    trigger: str = "webhook",
    base_ref: str | None,
    target_branch: str = "main",
) -> None:
    """A queued, never attempted Run of an enabled repository on ``PR_NEW``."""
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE repositories SET enabled=true, installation_available=true, "
                "branch_filter=:branch_filter WHERE id=:repo"
            ),
            {"branch_filter": branch_filter, "repo": REPO_A},
        )
        await session.execute(
            text("UPDATE code_changes SET target_branch=:target WHERE id=:pr"),
            {"target": target_branch, "pr": PR_NEW},
        )
        await session.execute(
            text("UPDATE runs SET attempt=0, trigger=:trigger, base_ref=:base WHERE id=:run"),
            {"trigger": trigger, "base": base_ref, "run": RUN_ATTEMPTED},
        )


async def _run_row(factory: async_sessionmaker[AsyncSession]) -> tuple[object, ...]:
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT state, attempt, error_code FROM runs WHERE id=:run"),
                {"run": RUN_ATTEMPTED},
            )
        ).one()
    return tuple(row)


@dataclass
class CheckRuns:
    views: list[CheckRunView] = field(default_factory=list)

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        self.views.append(view)


@dataclass
class RetryQueue:
    messages: list[tuple[PendingRunMessage, str]] = field(default_factory=list)

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        self.messages.append((message, delay_key))


class UnusedQueue:
    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        raise AssertionError("RunGuard publishes nothing when it skips a Run")

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        raise AssertionError("RunGuard publishes nothing when it skips a Run")

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        raise AssertionError("RunGuard publishes nothing when it skips a Run")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("trigger", "base_ref", "target_branch", "row", "pipeline_calls"),
    [
        ("webhook", "feature/x", "main", ("skipped", 0, "rule_not_matched"), 0),
        ("rerun", "feature/x", "main", ("skipped", 0, "rule_not_matched"), 0),
        # Without ``run.base_ref`` the PR target branch is compared.
        ("webhook", None, "feature/x", ("skipped", 0, "rule_not_matched"), 0),
        ("webhook", "release/1.2", "main", ("running", 1, None), 1),
    ],
)
def test_run_guard_skips_a_run_whose_target_branch_is_outside_the_filter(
    selection_env: Env,
    trigger: str,
    base_ref: str | None,
    target_branch: str,
    row: tuple[object, ...],
    pipeline_calls: int,
) -> None:
    async def exercise() -> None:
        engine = selection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[ClaimedAttempt] = []
        check_runs, retry = CheckRuns(), RetryQueue()

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            calls.append(claimed)
            return True

        try:
            await _prepare(
                factory,
                branch_filter=_FILTER,
                trigger=trigger,
                base_ref=base_ref,
                target_branch=target_branch,
            )
            now = datetime.now(UTC) + timedelta(seconds=10)
            handler = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                pipeline=pipeline,
                retry_queue=retry,
                check_runs=check_runs,
                worker_id="issue149-test",
                selection=SqlAlchemyBranchSelectionRule(factory),
                now=lambda: now,
            )
            assert await handler.execute(RUN_ATTEMPTED) is DeliveryOutcome.ACK
            assert await _run_row(factory) == row
            assert len(calls) == pipeline_calls
            assert retry.messages == []
            views = [(v.status, v.conclusion, v.summary) for v in check_runs.views]
            if pipeline_calls == 0:
                assert views == [("completed", "skipped", _REASON)]
            else:
                assert all(view.conclusion != "skipped" for view in check_runs.views)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_branch_selection_rule_selects_every_branch_without_a_filter_or_a_run(
    selection_env: Env,
) -> None:
    async def exercise() -> None:
        engine = selection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        rule = SqlAlchemyBranchSelectionRule(factory)
        try:
            await _prepare(factory, branch_filter=[], base_ref="feature/x")
            assert await rule.matches(RUN_ATTEMPTED) is True
            # A missing Run is left to RunGuard, which acks it.
            assert await rule.matches(uuid4()) is True
            await _prepare(factory, branch_filter=_FILTER, base_ref="feature/x")
            assert await rule.matches(RUN_ATTEMPTED) is False
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_worker_process_skips_a_run_outside_the_branch_filter(selection_env: Env) -> None:
    async def exercise() -> None:
        engine = selection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        settings = WorkerSettings.from_environment(
            {"DATABASE_URL": selection_env.database_url, "RABBITMQ_URL": "amqp://unused"}
        )
        now = datetime.now(UTC) + timedelta(seconds=10)
        process = compose_worker_process(
            settings=settings,
            session_factory=factory,
            queue=UnusedQueue(),
            github=None,
            now=lambda: now,
        )
        try:
            await _prepare(factory, branch_filter=_FILTER, base_ref="feature/x")
            assert await process.handle_run.execute(RUN_ATTEMPTED) is DeliveryOutcome.ACK
            assert await _run_row(factory) == ("skipped", 0, "rule_not_matched")
        finally:
            await engine.dispose()

    asyncio.run(exercise())
