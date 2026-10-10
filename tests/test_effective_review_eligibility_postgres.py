"""Public PG and application seams for preference AND App availability."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.determine_ci_eligibility import (
    CiProviderCheckpoint,
    CiSnapshot,
    DetermineCiEligibility,
)
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
)
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestProjectionStatus,
    PullRequestRepositoryRefused,
    PullRequestState,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionStore,
)
from app.modules.reviews.infrastructure.no_ci_sweep_candidates import SqlAlchemyDueNoCiCandidates
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleUnitOfWork
from app.modules.reviews.infrastructure.webhook_runs import (
    SqlAlchemyWebhookRunStore,
    SqlAlchemyWebhookRunUnitOfWork,
)
from tests.portal_postgres import (
    PR_NEW,
    PR_OPEN,
    REPO_A,
    RUN_ATTEMPTED,
    RUN_DONE,
    WS_A,
    Env,
    api,
    portal_schema,
    scalar,
)

_HEAD = "a" * 40
_NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


@pytest.fixture
def eligibility_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


async def _prepare(
    factory: async_sessionmaker[AsyncSession], preference: bool, available: bool
) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE repositories SET enabled=:preference, installation_available=:available "
                "WHERE id=:repo"
            ),
            {"preference": preference, "available": available, "repo": REPO_A},
        )
        await session.execute(
            text(
                "UPDATE code_changes SET head_sha=:head, ai_review_labeled=true, "
                "ai_review_labeled_at=:at, head_first_seen_at=:at WHERE id=:pr"
            ),
            {"head": _HEAD, "at": _NOW - timedelta(minutes=3), "pr": PR_OPEN},
        )


@dataclass
class FakeCi:
    calls: list[tuple[int, str, str]] = field(default_factory=list)

    async def get_current_head_ci(
        self,
        installation_external_id: int,
        repository_full_name: str,
        head_sha: str,
        *,
        checkpoint: CiProviderCheckpoint,
    ) -> CiSnapshot:
        self.calls.append((installation_external_id, repository_full_name, head_sha))
        return CiSnapshot(_HEAD, (), "pending", 0)


@dataclass
class Publisher:
    messages: list[PendingRunMessage] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        self.messages.append(message)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("preference", "available", "status", "ci_calls", "published", "run_count"),
    [
        (True, True, "enqueued", 1, 1, 5),
        (True, False, "ineligible", 0, 0, 4),
        (False, True, "ineligible", 0, 0, 4),
        (False, False, "ineligible", 0, 0, 4),
    ],
)
def test_ci_trigger_omits_paused_or_unavailable_repository_before_provider_calls(
    eligibility_env: Env,
    preference: bool,
    available: bool,
    status: str,
    ci_calls: int,
    published: int,
    run_count: int,
) -> None:
    async def exercise() -> None:
        engine = eligibility_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ci, publisher = FakeCi(), Publisher()
        try:
            await _prepare(factory, preference, available)
            eligibility = DetermineCiEligibility(
                candidates=SqlAlchemyEligibilityCandidateStore(factory),
                ci=ci,
                own_app_id=42,
                now=lambda: _NOW,
            )
            trigger = TryEnqueueWebhookRun(
                eligibility=eligibility,
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                publisher=publisher,
                now=lambda: _NOW,
            )
            result = await trigger.execute(PR_OPEN, _HEAD)
            assert result.status.value == status
            assert len(ci.calls) == ci_calls
            assert len(publisher.messages) == published
            if status == "ineligible":
                assert result.reason == "disabled_repository"
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == run_count
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("preference", "available", "eligible"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_locked_trigger_and_pending_publication_use_effective_repository_eligibility(
    eligibility_env: Env,
    preference: bool,
    available: bool,
    eligible: bool,
) -> None:
    async def exercise() -> None:
        engine = eligibility_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _prepare(factory, preference, available)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE code_changes SET ai_review_labeled=true WHERE id=:pr"),
                    {"pr": PR_NEW},
                )
            async with factory() as session:
                store = SqlAlchemyWebhookRunStore(session)
                candidate = await store.lock_candidate(PR_NEW)
                assert isinstance(candidate, RunInsertCandidate)
                assert candidate.ci.repository_enabled is eligible
                messages = await store.pending_messages(100)
                assert [message.run_id for message in messages] == (
                    [RUN_ATTEMPTED] if eligible else []
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("preference", "available", "eligible"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_no_ci_sweep_omits_paused_or_unavailable_repositories(
    eligibility_env: Env,
    preference: bool,
    available: bool,
    eligible: bool,
) -> None:
    async def exercise() -> None:
        engine = eligibility_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _prepare(factory, preference, available)
            async with factory() as session:
                candidates = await SqlAlchemyDueNoCiCandidates(session).list_due(_NOW, 100)
                assert [
                    (candidate.code_change_id, candidate.head_sha) for candidate in candidates
                ] == ([(PR_OPEN, _HEAD)] if eligible else [])
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("preference", "available", "eligible"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_pr_projection_refuses_paused_or_unavailable_repository_and_keeps_existing_history(
    eligibility_env: Env,
    preference: bool,
    available: bool,
    eligible: bool,
) -> None:
    event = PullRequestEvent(
        action="opened",
        installation_external_id=17,
        repository_external_id=101,
        external_id=99001,
        number=99,
        title="New PR",
        description=None,
        author_login="octocat",
        web_url="https://github.test/octo/a/pull/99",
        source_branch="feature",
        target_branch="main",
        base_sha="b" * 40,
        head_sha=_HEAD,
        state=PullRequestState.OPEN,
        provider_updated_at=_NOW,
    )

    async def exercise() -> None:
        engine = eligibility_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _prepare(factory, preference, available)
            async with factory.begin() as session:
                store = SqlAlchemyPullRequestProjectionStore(session)
                if eligible:
                    projected = await store.get_or_create_locked(event, _NOW)
                    assert projected is not None and projected.created is True
                    assert projected.record.repository_id == REPO_A
                else:
                    with pytest.raises(PullRequestRepositoryRefused) as refused:
                        await store.get_or_create_locked(event, _NOW)
                    assert refused.value.status is PullRequestProjectionStatus.DISABLED_REPOSITORY
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == (
                    5 if eligible else 4
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("preference", "available", "status", "run_count"),
    [(True, True, 202, 5), (True, False, 409, 4), (False, True, 409, 4), (False, False, 409, 4)],
)
def test_user_rerun_refuses_paused_or_unavailable_repo_without_hiding_readable_history(
    eligibility_env: Env,
    preference: bool,
    available: bool,
    status: int,
    run_count: int,
) -> None:
    with api(eligibility_env, AuthScope(42, (WS_A,))) as (client, factory):
        asyncio.run(_prepare(factory, preference, available))
        assert client.get(f"/api/repos/{REPO_A}").status_code == 200
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 200
        response = client.post(f"/api/runs/{RUN_DONE}/rerun")
        assert response.status_code == status
        assert tuple(scalar(factory, "SELECT count(*) FROM runs")) == (run_count,)
        if status == 409:
            assert response.json()["detail"] == (
                "the pull request has an active run or is closed, "
                "or repository reviews are paused or unavailable"
            )


@dataclass
class RetryQueue:
    messages: list[tuple[PendingRunMessage, str]] = field(default_factory=list)

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        self.messages.append((message, delay_key))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("preference", "available", "state", "attempt", "error", "pipeline_calls"),
    [
        (True, True, "running", 2, None, 1),
        (True, False, "skipped", 1, "repo_disabled", 0),
        (False, True, "skipped", 1, "repo_disabled", 0),
        (False, False, "skipped", 1, "repo_disabled", 0),
    ],
)
def test_worker_refuses_paused_or_unavailable_repo_before_pipeline(
    eligibility_env: Env,
    preference: bool,
    available: bool,
    state: str,
    attempt: int,
    error: str | None,
    pipeline_calls: int,
) -> None:
    async def exercise() -> None:
        engine = eligibility_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[ClaimedAttempt] = []
        retry = RetryQueue()

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            calls.append(claimed)
            return True

        try:
            await _prepare(factory, preference, available)
            now = datetime.now(UTC) + timedelta(seconds=10)
            handler = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                pipeline=pipeline,
                retry_queue=retry,
                check_runs=None,
                worker_id="issue113-test",
                now=lambda: now,
            )
            assert await handler.execute(RUN_ATTEMPTED) is DeliveryOutcome.ACK
            assert len(calls) == pipeline_calls
            assert retry.messages == []
            async with factory() as session:
                row = (
                    await session.execute(
                        text("SELECT state, attempt, error_code FROM runs WHERE id=:run"),
                        {"run": RUN_ATTEMPTED},
                    )
                ).one()
                assert tuple(row) == (state, attempt, error)
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())
