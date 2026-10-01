"""POST /api/runs/{id}/rerun against PostgreSQL and RabbitMQ (#34, T3)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import text

from app.modules.auth.application.scope import AuthScope
from tests.portal_postgres import (
    HEAD,
    RULE_A,
    RUN_B,
    RUN_CLOSED,
    RUN_DONE,
    WS_A,
    Env,
    api,
    portal_schema,
    queued_messages,
    scalar,
)


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL") or (
        os.environ.get("RABBITMQ_URL") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    )
    if database_url is None or rabbitmq_url is None:
        pytest.skip("set TEST_DATABASE_URL and TEST_RABBITMQ_URL to run publication tests")
    with portal_schema(database_url, rabbitmq_url) as schema:
        yield schema


@pytest.mark.integration
def test_rerun_creates_a_priority_nine_run_and_rejects_active_or_closed_prs(env: Env) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        created = client.post(f"/api/runs/{RUN_DONE}/rerun")
        again = client.post(f"/api/runs/{RUN_DONE}/rerun")
        closed = client.post(f"/api/runs/{RUN_CLOSED}/rerun")
        foreign = client.post(f"/api/runs/{RUN_B}/rerun")
        new_id = created.json()["id"]
        row = scalar(
            factory,
            "SELECT trigger, state, attempt, head_sha, message_published_at IS NOT NULL "
            "FROM runs WHERE id = :id",
            id=UUID(new_id),
        )
        runs = scalar(factory, "SELECT count(*) FROM runs")

    messages = asyncio.run(queued_messages(env))

    assert created.status_code == 202 and created.json()["status"] == "queued"
    assert tuple(row) == ("rerun", "queued", 0, HEAD, True)
    assert again.status_code == 409 and closed.status_code == 409
    assert foreign.status_code == 404
    assert tuple(runs) == (5,)
    assert [(priority, body["run_id"], body["trigger"]) for priority, body in messages] == [
        (9, new_id, "rerun")
    ]


@pytest.mark.integration
def test_rerun_without_an_active_rule_version_is_not_a_conflict_and_creates_no_run(
    env: Env,
) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):

        async def deactivate_rules() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE rule_versions SET is_active = false WHERE id = :id"),
                    {"id": RULE_A},
                )

        asyncio.run(deactivate_rules())
        response = client.post(f"/api/runs/{RUN_DONE}/rerun")
        runs = scalar(factory, "SELECT count(*) FROM runs")

    assert response.status_code == 422
    assert tuple(runs) == (4,)
    assert asyncio.run(queued_messages(env)) == []
