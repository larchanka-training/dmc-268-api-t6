"""POST /api/runs/{id}/cancel publishes the T6 close signal (#34)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest

from app.modules.auth.application.scope import AuthScope
from tests.portal_postgres import (
    RUN_ATTEMPTED,
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
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL")
    if database_url is None or rabbitmq_url is None:
        pytest.skip("set TEST_DATABASE_URL and TEST_RABBITMQ_URL to run publication tests")
    with portal_schema(database_url, rabbitmq_url) as schema:
        yield schema


@pytest.mark.integration
def test_cancel_of_an_attempted_queued_run_publishes_the_t6_close_signal(env: Env) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        response = client.post(f"/api/runs/{RUN_ATTEMPTED}/cancel")
        row = scalar(
            factory,
            "SELECT state, error_code, cancellation_signal_published_at IS NOT NULL "
            "FROM runs WHERE id = :id",
            id=RUN_ATTEMPTED,
        )

    messages = asyncio.run(queued_messages(env))

    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert tuple(row) == ("cancelled", "cancelled_by_user", True)
    assert [(priority, body["run_id"], body["attempt"]) for priority, body in messages] == [
        (9, str(RUN_ATTEMPTED), 2)
    ]
