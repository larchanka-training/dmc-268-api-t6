"""Worker NOTIFY run_updated reaches the /api/stream hub (#34, D12)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.run_update_listener import listen_forever
from app.modules.reviews.application.run_events import InMemoryRunUpdateHub, RunUpdated
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)
from tests.portal_postgres import (
    HEAD,
    NOW,
    PR_OPEN,
    PROMPT,
    RULE_A,
    Env,
    portal_schema,
)


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


async def insert_queued_run(factory: async_sessionmaker[AsyncSession]) -> UUID:
    """A queued Run with an id of its own: ``run_updated`` is database-wide, seeded ids are not.

    PR_NEW already holds an active Run and PR_OPEN a webhook Run on HEAD, hence a manual one.
    """
    run_id = uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, state, "
                "trigger, idempotency_key, engine, rule_version_id, prompt_version_id, "
                "available_at, attempt, created_at) VALUES (:id, :pr, :base, 'main', :head, "
                "'queued', 'manual', :key, 'fast', :rule, :prompt, :now, 0, :now)"
            ),
            {
                "id": run_id,
                "pr": PR_OPEN,
                "base": "b" * 40,
                "head": HEAD,
                "key": uuid4().hex + uuid4().hex,
                "rule": RULE_A,
                "prompt": PROMPT,
                "now": NOW,
            },
        )
    return run_id


@pytest.mark.integration
def test_worker_status_change_reaches_the_sse_hub_through_listen_notify(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    hub = InMemoryRunUpdateHub()
    database_url = f"{env.database_url}?options=-csearch_path%3D{env.schema}"

    async def scenario() -> tuple[UUID, list[RunUpdated]]:
        run_id = await insert_queued_run(factory)
        received: list[RunUpdated] = []
        async with hub.subscribe() as events:
            listener = asyncio.create_task(listen_forever(database_url, hub))
            await asyncio.sleep(0.5)
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                await uow.runs.finish(
                    run_id,
                    from_state="queued",
                    worker_id=None,
                    state="skipped",
                    error_code="repo_disabled",
                    error_message=None,
                    now=NOW,
                )
                await uow.rollback()
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                await uow.runs.claim(
                    run_id, worker_id="w", now=NOW, lease_until=NOW + timedelta(minutes=5)
                )
                await uow.commit()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    while True:
                        received.append(await anext(events))
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        await engine.dispose()
        return run_id, received

    run_id, received = asyncio.run(scenario())
    # Another test run against the same database notifies on the same channel.
    assert [event for event in received if event.run_id == run_id] == [
        RunUpdated(run_id, "running")
    ]
