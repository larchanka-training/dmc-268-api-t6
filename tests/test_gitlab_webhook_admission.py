"""Pure API opt-in and actual main lifespan/ASGI admission over owned PostgreSQL."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.main import app, lifespan
from app.modules.integrations.gitlab.infrastructure.settings import (
    GitLabApiSettings,
    GitLabSettingsError,
)
from tests import test_gitlab_webhook_auth_postgres as auth
from tests.test_gitlab_webhook_delivery import BODY, HEADERS

admission_database = auth.database


@pytest.mark.parametrize(
    "value,enabled",
    [
        (None, False),
        ("", False),
        ("  ", False),
        ("false", False),
        (" FALSE ", False),
        ("true", True),
        (" TrUe ", True),
    ],
)
def test_admission_flag_is_pure_and_strict(value: str | None, enabled: bool) -> None:
    env = {
        "GITLAB_CREDENTIAL_KEYS": "invalid",
        "SSL_CERT_FILE": "/missing.pem",
        "GITLAB_TOKEN": "legacy",
    }
    if value is not None:
        env["GITLAB_WEBHOOK_INTAKE_ENABLED"] = value
    assert GitLabApiSettings.from_environment(env).intake_enabled is enabled


@pytest.mark.parametrize("value", ["0", "1", "yes", "on", "secret-canary"])
def test_invalid_flag_fails_actual_main_before_resource_allocation(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("GITLAB_WEBHOOK_INTAKE_ENABLED", value)
    monkeypatch.setenv("DATABASE_URL", "this-would-fail-resource-startup")

    async def scenario() -> None:
        with pytest.raises(GitLabSettingsError, match="^configuration_invalid$"):
            async with lifespan(app):
                raise AssertionError("invalid flag must not start")
        assert not hasattr(app.state, "reviews_api_resources")
        assert not hasattr(app.state, "github_auth_http_clients")

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    "flag,state,status",
    [
        (None, "enabled", 503),
        ("false", "enabled", 503),
        ("true", "enabled", 202),
        ("true", "disabled", 503),
        ("true", "revoked", 401),
        ("true", "unbound", 400),
    ],
)
def test_actual_main_admits_only_opted_in_and_authenticated_connections(
    admission_database: auth.Database,
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
    state: str,
    status: int,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        f"{admission_database.url}?options=-csearch_path%3D{admission_database.schema}",
    )
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    for name in (
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
        "GITLAB_TOKEN",
        "SSL_CERT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    if flag is None:
        monkeypatch.delenv("GITLAB_WEBHOOK_INTAKE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("GITLAB_WEBHOOK_INTAKE_ENABLED", flag)

    async def scenario() -> None:
        async with admission_database.sessions() as sessions:
            async with sessions.begin() as session:
                if state == "disabled":
                    await session.execute(
                        text(
                            "UPDATE gitlab_connections SET intake_enabled=false "
                            "WHERE provider_installation_id=:id"
                        ),
                        {"id": auth.CONNECTION},
                    )
                elif state == "revoked":
                    await session.execute(
                        text(
                            "UPDATE gitlab_connections SET intake_enabled=false,revoked_at=now() "
                            "WHERE provider_installation_id=:id"
                        ),
                        {"id": auth.CONNECTION},
                    )
                elif state == "unbound":
                    await session.execute(
                        text(
                            "DELETE FROM gitlab_project_bindings WHERE provider_installation_id=:id"
                        ),
                        {"id": auth.CONNECTION},
                    )
            async with lifespan(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        f"/webhooks/gitlab/{auth.CONNECTION}",
                        content=BODY,
                        headers={**HEADERS, "webhook-id": str(uuid4())},
                    )
                assert response.status_code == status, response.text
            assert not hasattr(app.state, "gitlab_webhook_resources")
            async with sessions() as session:
                count = await session.scalar(text("SELECT count(*) FROM webhook_events"))
                assert count == (1 if status == 202 else 0)

    asyncio.run(scenario())
