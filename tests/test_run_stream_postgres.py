"""Worker NOTIFY run_updated reaches the /api/stream hub (#34, D12)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bootstrap.run_update_listener import listen_forever
from app.modules.reviews.application.run_events import InMemoryRunUpdateHub, RunUpdated
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)
from tests.portal_postgres import (
    NOW,
    RUN_ATTEMPTED,
    Env,
    rest_env,  # noqa: F401  (the env fixture)
)


@pytest.mark.integration
def test_worker_status_change_reaches_the_sse_hub_through_listen_notify(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    hub = InMemoryRunUpdateHub()
    database_url = f"{env.database_url}?options=-csearch_path%3D{env.schema}"

    async def scenario() -> list[RunUpdated]:
        received: list[RunUpdated] = []
        async with hub.subscribe() as events:
            listener = asyncio.create_task(listen_forever(database_url, hub))
            await asyncio.sleep(0.5)
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                await uow.runs.finish(
                    RUN_ATTEMPTED,
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
                    RUN_ATTEMPTED, worker_id="w", now=NOW, lease_until=NOW + timedelta(minutes=5)
                )
                await uow.commit()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    while True:
                        received.append(await anext(events))
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        await engine.dispose()
        return received

    assert asyncio.run(scenario()) == [RunUpdated(RUN_ATTEMPTED, "running")]
