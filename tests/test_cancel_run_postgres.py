"""POST /api/runs/{id}/cancel publishes the T6 close signal (#34)."""

from __future__ import annotations

import asyncio

import pytest

from app.modules.auth.application.scope import AuthScope
from tests.portal_postgres import (
    RUN_ATTEMPTED,
    WS_A,
    Env,
    api,
    queued_messages,
    rest_env,  # noqa: F401  (the env fixture)
    scalar,
)


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

    messages = asyncio.run(queued_messages(env.rabbitmq_url))

    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert tuple(row) == ("cancelled", "cancelled_by_user", True)
    assert [(priority, body["run_id"], body["attempt"]) for priority, body in messages] == [
        (9, str(RUN_ATTEMPTED), 2)
    ]
