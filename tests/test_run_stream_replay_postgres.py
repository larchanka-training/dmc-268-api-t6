"""GET /api/stream replays the user's runs changed after Last-Event-ID (ui#74, AC 2.16)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.main as main_mod
from app.main import app, get_run_event_hub
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.run_events import RunUpdated, run_event_id
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import (
    NOW,
    RUN_ATTEMPTED,
    RUN_B,
    RUN_CLOSED,
    RUN_DONE,
    WS_A,
    Env,
    api,
    portal_schema,
)

# User 42 sees only Workspace A: RUN_B (Workspace B) is hidden by the claim.
SCOPE = AuthScope(42, (WS_A,))


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


class SilentHub:
    """No live events: the response holds the replay only, and the stream ends."""

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[RunUpdated]]:
        async def events() -> AsyncIterator[RunUpdated]:
            silence: list[RunUpdated] = []
            for event in silence:
                yield event

        yield events()


def _set_updated_at(factory: async_sessionmaker[AsyncSession], at: dict[UUID, datetime]) -> None:
    async def write() -> None:
        async with factory() as session:
            for run_id, value in at.items():
                await session.execute(
                    text("UPDATE runs SET updated_at = :at WHERE id = :id"),
                    {"at": value, "id": run_id},
                )
            await session.commit()

    asyncio.run(write())


def _replayed(client: Any, since: datetime) -> list[tuple[str, str, str]]:
    app.dependency_overrides[get_run_event_hub] = SilentHub
    response = client.get("/api/stream", headers={"Last-Event-ID": run_event_id(since)})
    assert response.status_code == 200
    frames = [frame for frame in response.text.split("\n\n") if frame]
    fields = [dict(line.split(": ", 1) for line in frame.split("\n")) for frame in frames]
    return [
        (item["id"], json.loads(item["data"])["runId"], json.loads(item["data"])["status"])
        for item in fields
    ]


@pytest.mark.integration
def test_replay_is_scoped_oldest_first_and_reaches_back_30_seconds(env: Env) -> None:
    since = NOW
    with api(env, SCOPE) as (client, factory):
        _set_updated_at(
            factory,
            {
                RUN_CLOSED: since - timedelta(seconds=20),  # inside the overlap window
                RUN_ATTEMPTED: since - timedelta(seconds=20),  # same instant: ordered by id
                RUN_DONE: since + timedelta(seconds=5),
                RUN_B: since + timedelta(seconds=10),  # newer, but another Workspace
            },
        )
        replayed = _replayed(client, since)

    assert replayed == [
        (run_event_id(since - timedelta(seconds=20)), str(RUN_CLOSED), "succeeded"),
        (run_event_id(since - timedelta(seconds=20)), str(RUN_ATTEMPTED), "queued"),
        (run_event_id(since + timedelta(seconds=5)), str(RUN_DONE), "succeeded"),
    ]


@pytest.mark.integration
def test_replay_skips_runs_older_than_the_window_and_keeps_the_newest_on_overflow(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    since = NOW
    with api(env, SCOPE) as (client, factory):
        _set_updated_at(
            factory,
            {
                RUN_DONE: since - timedelta(seconds=120),  # outside the window
                RUN_CLOSED: since - timedelta(seconds=20),
                RUN_ATTEMPTED: since + timedelta(seconds=5),
            },
        )
        within_cap = _replayed(client, since)
        monkeypatch.setattr(main_mod, "REPLAY_LIMIT", 1)
        over_cap = _replayed(client, since)

    assert [run_id for _, run_id, _ in within_cap] == [str(RUN_CLOSED), str(RUN_ATTEMPTED)]
    assert [run_id for _, run_id, _ in over_cap] == [str(RUN_ATTEMPTED)]


@pytest.mark.integration
def test_replay_window_edge_is_exclusive_to_the_microsecond(env: Env) -> None:
    since = NOW
    edge = since - timedelta(seconds=30)
    with api(env, SCOPE) as (client, factory):
        _set_updated_at(
            factory,
            {
                RUN_DONE: edge,  # exactly at the edge: `updated_at > edge` leaves it out
                RUN_ATTEMPTED: edge - timedelta(microseconds=1),
                RUN_CLOSED: edge + timedelta(microseconds=1),
            },
        )
        replayed = _replayed(client, since)

    assert replayed == [
        (run_event_id(edge + timedelta(microseconds=1)), str(RUN_CLOSED), "succeeded"),
    ]


@pytest.mark.integration
def test_run_updated_at_returns_the_stored_value_only_for_visible_runs(env: Env) -> None:
    updated_at = NOW - timedelta(minutes=7)
    with api(env, SCOPE) as (_, factory):
        _set_updated_at(factory, {RUN_DONE: updated_at, RUN_B: updated_at})
        repository = SqlAlchemyRunRepository(factory, SCOPE)
        visible = asyncio.run(repository.run_updated_at(RUN_DONE))
        hidden = asyncio.run(repository.run_updated_at(RUN_B))

    assert visible == updated_at
    assert hidden is None
