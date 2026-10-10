"""Permanent retirement at CI and locked enqueue boundaries, independent of flags."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Connection, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    CiProviderCheckpoint,
    CiSnapshot,
    DetermineCiEligibility,
)
from app.modules.reviews.application.rerun_run import RerunOutcome
from app.modules.reviews.application.try_enqueue_webhook_run import (
    RunInsertCandidate,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.github_ci import HttpGitHubCurrentHeadCiProvider
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunStore
from app.modules.reviews.infrastructure.webhook_runs import (
    SqlAlchemyWebhookRunStore,
    SqlAlchemyWebhookRunUnitOfWork,
)
from tests.portal_postgres import INSTALL_A, PR_OPEN, REPO_A, RUN_DONE, WS_A, Env, portal_schema
from tests.test_effective_review_eligibility_postgres import Publisher
from tests.test_retired_installation_postgres import DiscardTokenCache

_HEAD = "a" * 40
_NOW = datetime.now(UTC)


@pytest.fixture
def enqueue_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def prepare(factory: async_sessionmaker[AsyncSession], wait_mode: str, retired: bool) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE repositories SET enabled=true,installation_available=true, "
                "wait_for_ci=:mode WHERE id=:id"
            ),
            {"mode": wait_mode, "id": REPO_A},
        )
        await session.execute(
            text(
                "UPDATE code_changes SET head_sha=:head,ai_review_labeled=true, "
                "ai_review_labeled_at=:now,head_first_seen_at=:now WHERE id=:id"
            ),
            {"head": _HEAD, "now": _NOW, "id": PR_OPEN},
        )
        if retired:
            await session.execute(
                text("UPDATE provider_installations SET deleted_at=clock_timestamp() WHERE id=:id"),
                {"id": INSTALL_A},
            )


@dataclass
class Ci:
    factory: async_sessionmaker[AsyncSession]
    calls: list[int] = field(default_factory=list)

    async def get_current_head_ci(
        self,
        installation_external_id: int,
        repository_full_name: str,
        head_sha: str,
        *,
        checkpoint: CiProviderCheckpoint,
    ) -> CiSnapshot:
        async with self.factory.begin() as session:
            await session.execute(
                text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": INSTALL_A},
            )
        self.calls.append(installation_external_id)
        return CiSnapshot(_HEAD, (), "success", 1)


@pytest.mark.integration
@pytest.mark.parametrize("wait_mode", ["never", "always"])
@pytest.mark.parametrize("retired", [False, True])
def test_retired_source_is_ineligible_before_ci_or_run_even_with_positive_repository_flags(
    enqueue_env: Env,
    wait_mode: str,
    retired: bool,
) -> None:
    async def exercise() -> None:
        engine = enqueue_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ci, publisher = Ci(factory), Publisher()
        try:
            await prepare(factory, wait_mode, retired)
            enqueue = TryEnqueueWebhookRun(
                eligibility=DetermineCiEligibility(
                    candidates=SqlAlchemyEligibilityCandidateStore(factory),
                    ci=ci,
                    own_app_id=42,
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                publisher=publisher,
            )
            result = await enqueue.execute(PR_OPEN, _HEAD)
            assert result.status.value == ("ineligible" if retired else "enqueued")
            if retired:
                assert result.reason == "disabled_repository"
            assert ci.calls == ([17] if not retired and wait_mode == "always" else [])
            assert len(publisher.messages) == (0 if retired else 1)
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == (
                    4 if retired else 5
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@dataclass
class RetiringEligibility:
    real: DetermineCiEligibility
    factory: async_sessionmaker[AsyncSession]

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        decision = await self.real.execute(code_change_id, expected_head_sha)
        assert decision.eligible is True
        async with self.factory.begin() as session:
            await session.execute(
                text("UPDATE provider_installations SET deleted_at=clock_timestamp() WHERE id=:id"),
                {"id": INSTALL_A},
            )
        return decision


@pytest.mark.integration
def test_retirement_between_detached_decision_and_locked_enqueue_refuses_run(
    enqueue_env: Env,
) -> None:
    async def exercise() -> None:
        engine = enqueue_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ci, publisher = Ci(factory), Publisher()
        try:
            await prepare(factory, "never", False)
            enqueue = TryEnqueueWebhookRun(
                eligibility=RetiringEligibility(
                    DetermineCiEligibility(
                        candidates=SqlAlchemyEligibilityCandidateStore(factory),
                        ci=ci,
                        own_app_id=42,
                    ),
                    factory,
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                publisher=publisher,
            )
            result = await enqueue.execute(PR_OPEN, _HEAD)
            assert (result.status.value, result.reason) == ("stale", "state_changed")
            assert ci.calls == [] and publisher.messages == []
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_locked_enqueue_holds_installation_before_inserting_run(enqueue_env: Env) -> None:
    async def exercise() -> None:
        engine = enqueue_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "never", False)
            async with factory.begin() as session:
                candidate = await SqlAlchemyWebhookRunStore(session).lock_candidate(PR_OPEN)
                assert isinstance(candidate, RunInsertCandidate)
                async with factory.begin() as concurrent:
                    with pytest.raises(DBAPIError) as blocked:
                        await concurrent.execute(
                            text(
                                "SELECT id FROM provider_installations WHERE id=:id FOR UPDATE "
                                "NOWAIT"
                            ),
                            {"id": INSTALL_A},
                        )
                    assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_rerun_waits_for_source_before_taking_pr_lock_and_retains_normal_creation(
    enqueue_env: Env,
) -> None:
    async def exercise() -> None:
        engine = enqueue_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        source_started = asyncio.Event()
        pending: asyncio.Task[RerunOutcome] | None = None

        def before_query(
            connection: Connection,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            if (
                "FOR NO KEY UPDATE" in statement
                and "provider_installations" in statement
                and "code_changes" in statement
            ):
                source_started.set()

        async def rerun() -> RerunOutcome:
            async with factory.begin() as session:
                result = await SqlAlchemyRerunStore(session, AuthScope(42, (WS_A,))).create_rerun(
                    RUN_DONE,
                    _NOW,
                )
                return result.outcome

        event.listen(engine.sync_engine, "before_cursor_execute", before_query)
        try:
            async with factory.begin() as holder:
                await holder.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE"),
                    {"id": INSTALL_A},
                )
                pending = asyncio.create_task(rerun())
                await asyncio.wait_for(source_started.wait(), timeout=5)
                # The rerun is waiting on source; a competing source-first writer must
                # still be able to acquire the PR, preventing a PI ↔ PR lock cycle.
                await holder.execute(
                    text("SELECT id FROM code_changes WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": PR_OPEN},
                )
            assert await pending is RerunOutcome.CREATED
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 5
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", before_query)
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    "closed,active,missing_rules,retired,outcome,run_count",
    [
        (True, False, True, False, RerunOutcome.CONFLICT, 4),
        (False, True, True, False, RerunOutcome.CONFLICT, 4),
        (False, False, True, False, RerunOutcome.NOT_CONFIGURED, 4),
        (False, False, False, False, RerunOutcome.CREATED, 5),
        (False, False, False, True, RerunOutcome.CONFLICT, 4),
    ],
)
def test_rerun_source_lock_keeps_conflict_precedence_and_retired_refusal(
    enqueue_env: Env,
    closed: bool,
    active: bool,
    missing_rules: bool,
    retired: bool,
    outcome: RerunOutcome,
    run_count: int,
) -> None:
    async def exercise() -> None:
        engine = enqueue_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "never", retired)
            async with factory.begin() as session:
                if closed:
                    await session.execute(
                        text("UPDATE code_changes SET state='closed' WHERE id=:id"), {"id": PR_OPEN}
                    )
                if active:
                    await session.execute(
                        text("UPDATE runs SET state='queued' WHERE id=:id"), {"id": RUN_DONE}
                    )
                if missing_rules:
                    await session.execute(
                        text("UPDATE rule_versions SET is_active=false WHERE repository_id=:id"),
                        {"id": REPO_A},
                    )
                result = await SqlAlchemyRerunStore(session, AuthScope(42, (WS_A,))).create_rerun(
                    RUN_DONE, _NOW
                )
                assert result.outcome is outcome
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == run_count
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["token", "page", "status"])
@pytest.mark.parametrize("retire", [False, True])
def test_ci_stops_next_http_after_retirement(enqueue_env: Env, phase: str, retire: bool) -> None:
    _exercise_ci_request_change(enqueue_env, phase, "retire" if retire else "active")


@pytest.mark.integration
@pytest.mark.parametrize("change", ["head", "source"])
def test_ci_pins_initial_head_and_source_before_next_page(enqueue_env: Env, change: str) -> None:
    _exercise_ci_request_change(enqueue_env, "page", change)


def _exercise_ci_request_change(env: Env, phase: str, change: str) -> None:
    async def exercise() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        publisher = Publisher()
        calls: list[str] = []

        async def mutate() -> None:
            async with factory.begin() as session:
                if change == "retire":
                    await session.execute(
                        text(
                            "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                            "WHERE id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
                elif change == "head":
                    await session.execute(
                        text("UPDATE code_changes SET head_sha=:head WHERE id=:id"),
                        {"id": PR_OPEN, "head": "b" * 40},
                    )
                elif change == "source":
                    await session.execute(
                        text(
                            "INSERT INTO provider_installations "
                            "(id,workspace_id,provider,external_id,metadata) "
                            "VALUES (:id,:ws,'github',29,'{}'::jsonb)"
                        ),
                        {"id": UUID("00000000-0000-4000-8000-000000000029"), "ws": WS_A},
                    )
                    await session.execute(
                        text(
                            "UPDATE repositories SET provider_installation_id=:source WHERE id=:id"
                        ),
                        {"id": REPO_A, "source": UUID("00000000-0000-4000-8000-000000000029")},
                    )

        async def http(request: httpx.Request) -> httpx.Response:
            # Any still-open candidate read would prevent this independent lock.
            async with factory.begin() as session:
                await session.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": INSTALL_A},
                )
            if request.url.path.endswith("/access_tokens"):
                calls.append("token")
                assert request.url.path == "/app/installations/17/access_tokens"
                if phase == "token":
                    await mutate()
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if request.url.path.endswith("/check-suites"):
                page = int(request.url.params["page"])
                calls.append(f"check{page}")
                if page == 1 and phase != "token":
                    await mutate()
                count = 101 if phase == "page" else 0
                indices = range(1, 101) if page == 1 else range(101, 102)
                suites = (
                    [
                        {
                            "id": index,
                            "head_sha": _HEAD,
                            "app": {"id": 7},
                            "status": "completed",
                            "conclusion": "success",
                        }
                        for index in indices
                    ]
                    if count
                    else []
                )
                return httpx.Response(200, json={"total_count": count, "check_suites": suites})
            assert request.url.path.endswith("/status")
            calls.append("status")
            return httpx.Response(200, json={"sha": _HEAD, "state": "success", "total_count": 1})

        try:
            await prepare(factory, "always", False)
            async with factory() as session:
                previous_ci = await session.scalar(
                    text("SELECT ci_status FROM code_changes WHERE id=:id"), {"id": PR_OPEN}
                )
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=time.time,
                )
                enqueue = TryEnqueueWebhookRun(
                    eligibility=DetermineCiEligibility(
                        candidates=SqlAlchemyEligibilityCandidateStore(factory),
                        ci=HttpGitHubCurrentHeadCiProvider(client=client, token_provider=tokens),
                        own_app_id=42,
                    ),
                    uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                    publisher=publisher,
                )
                # No explicit receipt hint: the provider must pin its original read even for sweep.
                result = await enqueue.execute(PR_OPEN, _HEAD)
            if change == "active":
                assert result.status.value == "enqueued"
                assert calls == (
                    ["token", "check1", "check2", "status"]
                    if phase == "page"
                    else ["token", "check1", "status"]
                )
                assert len(publisher.messages) == 1
            else:
                assert result.status.value == "ineligible"
                assert (
                    result.reason
                    == {
                        "retire": "disabled_repository",
                        "head": "stale_head",
                        "source": "stale_state",
                    }[change]
                )
                assert calls == (["token"] if phase == "token" else ["token", "check1"])
                assert publisher.messages == []
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == (
                    5 if change == "active" else 4
                )
                assert (
                    await session.scalar(
                        text("SELECT ci_status FROM code_changes WHERE id=:id"), {"id": PR_OPEN}
                    )
                    == previous_ci
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
