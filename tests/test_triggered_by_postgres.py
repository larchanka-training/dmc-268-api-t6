"""Nullable runs.triggered_by stays null until a writer sets it."""

from __future__ import annotations

import asyncio
import json
import os
import selectors
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.rerun_run import RerunOutcome, create_rerun
from app.modules.reviews.application.try_enqueue_webhook_run import (
    UNSET,
    PendingRunMessage,
    RunInsertCandidate,
    UnsetType,
)
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunStore
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from tests.portal_postgres import (
    NOW,
    PR_NEW,
    PR_OPEN,
    RUN_ATTEMPTED,
    RUN_DONE,
    WS_A,
    Env,
    portal_schema,
)

pytestmark = pytest.mark.integration

_FRESH = UUID("00000000-0000-4000-8000-000000003104")
_COLUMN = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'runs' "
    "AND column_name = 'triggered_by'"
)


@pytest.fixture
def triggered_by_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


def test_triggered_by_is_null_without_a_writer_and_survives_downgrade(
    triggered_by_env: Env,
) -> None:
    async def exercise() -> None:
        engine = triggered_by_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = SqlAlchemyRunRepository(factory, scope=AuthScope(42, (WS_A,)))
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, "
                        "state, trigger, idempotency_key, engine, rule_version_id, "
                        "prompt_version_id, available_at, attempt, created_at, "
                        "origin_provider_installation_id) "
                        "SELECT :fresh, code_change_id, base_sha, base_ref, head_sha, state, "
                        "'manual', :key, engine, rule_version_id, prompt_version_id, "
                        "available_at, attempt, created_at, origin_provider_installation_id "
                        "FROM runs WHERE id = :old"
                    ),
                    {"fresh": _FRESH, "key": "e" * 64, "old": RUN_DONE},
                )
            await _assert_unset(repository)
        finally:
            await engine.dispose()

        _downgrade_and_upgrade(triggered_by_env)

        engine = triggered_by_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = SqlAlchemyRunRepository(factory, scope=AuthScope(42, (WS_A,)))
        try:
            await _assert_unset(repository)
        finally:
            await engine.dispose()

    asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


async def _assert_unset(repository: SqlAlchemyRunRepository) -> None:
    items = await repository.list_runs(status=None, repository="octo/a", cursor=None, limit=10)
    fresh = next(item for item in items if item.id == _FRESH)
    seeded = next(item for item in items if item.id == RUN_DONE)
    assert fresh.triggered_by is None
    assert seeded.triggered_by is None


def _downgrade_and_upgrade(env: Env) -> None:
    sync = create_engine(env.database_url)
    try:
        with sync.connect() as connection:
            connection.execute(text(f'SET search_path TO "{env.schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.downgrade(config, "20261026_0043")
            assert connection.scalar(text(_COLUMN)) == 0
            connection.commit()
            command.upgrade(config, "head")
            assert connection.scalar(text(_COLUMN)) == 1
            connection.commit()
    finally:
        sync.dispose()


def test_triggered_by_records_an_explicit_actor_or_the_push_author(
    triggered_by_env: Env,
) -> None:
    async def exercise() -> None:
        engine = triggered_by_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state = 'succeeded' WHERE id = :id"),
                    {"id": RUN_ATTEMPTED},
                )
                await _insert_actor(
                    session,
                    head="1" * 40,
                    triggered_by="dependabot[bot]",
                    events=(),
                )
                await _insert_actor(
                    session,
                    head="2" * 40,
                    triggered_by=None,
                    events=(("pull_request", "synchronize", {"sender": {"login": "someone"}}),),
                )
                await _insert_actor(
                    session,
                    head="3" * 40,
                    triggered_by=UNSET,
                    events=(("pull_request", "synchronize", {"sender": {"login": "pusher"}}),),
                )
                await _insert_actor(
                    session,
                    head="4" * 40,
                    triggered_by=UNSET,
                    events=(("check_suite", "completed", {"sender": {"login": "ci-bot"}}),),
                )
                await _insert_actor(
                    session,
                    head="5" * 40,
                    triggered_by=UNSET,
                    events=(("pull_request", "synchronize", None),),
                )
                await _insert_actor(
                    session,
                    head="6" * 40,
                    triggered_by=UNSET,
                    events=(
                        (
                            "push",
                            None,
                            {"sender": {"login": "real-pusher"}, "after": "6" * 40},
                        ),
                        (
                            "pull_request",
                            "opened",
                            {"sender": {"login": "opened-author"}},
                        ),
                    ),
                )
                await _insert_actor(
                    session,
                    head="7" * 40,
                    triggered_by=UNSET,
                    events=(("pull_request", "opened", {"sender": {"login": "opened-author"}}),),
                )
                await _insert_actor(
                    session,
                    head="8" * 40,
                    triggered_by=UNSET,
                    provider="gitlab",
                    events=(
                        ("push", None, {"sender": {"login": "real-pusher"}, "after": "8" * 40}),
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


async def _insert_actor(
    session: AsyncSession,
    *,
    head: str,
    triggered_by: str | None | UnsetType,
    events: tuple[tuple[str, str | None, dict[str, object] | None], ...],
    provider: str = "github",
) -> None:
    await session.execute(
        text("UPDATE runs SET state = 'succeeded' WHERE code_change_id = :pr AND state = 'queued'"),
        {"pr": PR_NEW},
    )
    await session.execute(
        text("UPDATE code_changes SET head_sha = :head WHERE id = :pr"),
        {"head": head, "pr": PR_NEW},
    )
    store = SqlAlchemyWebhookRunStore(session)
    locked = await store.lock_candidate(PR_NEW)
    assert isinstance(locked, RunInsertCandidate)
    candidate = locked if provider == "github" else replace(locked, provider=provider)
    for index, (event, action, body) in enumerate(events):
        await session.execute(
            text(
                "INSERT INTO webhook_events (id, installation_external_id, delivery_id, event, "
                "action, payload, payload_s3_ref, received_at) VALUES (:id, :installation, "
                ":delivery, :event, :action, CAST(:payload AS jsonb), :ref, :received)"
            ),
            {
                "id": uuid4(),
                "installation": candidate.ci.installation_external_id,
                "delivery": f"{head}-{index}",
                "event": event,
                "action": action,
                "payload": None if body is None else json.dumps(_delivery(candidate, head, body)),
                "ref": None if body is not None else "s3://receipts/head",
                "received": NOW - timedelta(minutes=index),
            },
        )
    message = await store.insert_webhook_run(candidate, NOW, triggered_by=triggered_by)
    assert isinstance(message, PendingRunMessage)
    stored = await session.scalar(
        text("SELECT triggered_by FROM runs WHERE id = :id"),
        {"id": message.run_id},
    )
    expected = {
        "1" * 40: "dependabot[bot]",
        "2" * 40: None,
        "3" * 40: "pusher",
        "4" * 40: None,
        "5" * 40: None,
        "6" * 40: "real-pusher",
        "7" * 40: None,
        "8" * 40: None,
    }[head]
    assert stored == expected


def test_rerun_stores_the_portal_login_and_leaves_the_webhook_row_alone(
    triggered_by_env: Env,
) -> None:
    async def exercise() -> None:
        engine = triggered_by_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE github_user_profiles SET login = 'octocat' WHERE id = 42")
                )
                await session.execute(
                    text("UPDATE runs SET triggered_by = 'pusher' WHERE id = :id"),
                    {"id": RUN_DONE},
                )
                created = await create_rerun(
                    SqlAlchemyRerunStore(session, AuthScope(42, (WS_A,))), RUN_DONE, NOW
                )
                assert created.outcome is RerunOutcome.CREATED
                assert created.message is not None
                assert await _rerun_actor(session, created.message.run_id) == (
                    "rerun",
                    "octocat",
                )
                assert await _rerun_actor(session, RUN_DONE) == ("webhook", "pusher")

                await session.execute(
                    text("UPDATE runs SET state = 'succeeded' WHERE id = :id"),
                    {"id": created.message.run_id},
                )
                candidate = await SqlAlchemyWebhookRunStore(session).lock_candidate(PR_OPEN)
                assert isinstance(candidate, RunInsertCandidate)
                missing = await SqlAlchemyRerunStore(session, AuthScope(99, (WS_A,))).insert_rerun(
                    candidate, PR_OPEN, NOW
                )
                assert await _rerun_actor(session, missing.run_id) == ("rerun", None)
                await session.execute(
                    text("UPDATE runs SET state = 'succeeded' WHERE id = :id"),
                    {"id": missing.run_id},
                )
                unscoped = await SqlAlchemyRerunStore(session, allow_unscoped=True).insert_rerun(
                    candidate, PR_OPEN, NOW
                )
                assert await _rerun_actor(session, unscoped.run_id) == ("rerun", None)
                assert await _rerun_actor(session, RUN_DONE) == ("webhook", "pusher")
        finally:
            await engine.dispose()

    asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


async def _rerun_actor(session: AsyncSession, run_id: UUID) -> tuple[str, str | None]:
    row = (
        await session.execute(
            text("SELECT trigger, triggered_by FROM runs WHERE id = :id"),
            {"id": run_id},
        )
    ).one()
    trigger, login = row
    assert isinstance(trigger, str)
    assert login is None or isinstance(login, str)
    return trigger, login


def _delivery(
    candidate: RunInsertCandidate, head: str, body: dict[str, object]
) -> dict[str, object]:
    return {
        "repository": {"id": candidate.repository_external_id},
        "pull_request": {"head": {"sha": head}},
        **body,
    }
