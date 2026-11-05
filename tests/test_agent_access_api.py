"""Public device bootstrap and bearer-only token revocation contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from app.bootstrap.agent_access import get_agent_device_access, get_agent_tokens
from app.bootstrap.portal_auth import get_auth_scope
from app.main import app
from app.modules.auth.application.agent_device_access import DeviceChallenge, DeviceOutcome
from app.modules.auth.application.agent_tokens import IssuedAgentToken
from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.api.dtos import RepositoryUpdateDto


class FakeDeviceAccess:
    async def start(self, repository: str) -> DeviceChallenge:
        assert repository == "octo/repo"
        return DeviceChallenge(
            "device-secret", "ABCD-EFGH", "https://github.com/login/device", 900, 5
        )

    async def poll(self, code: str) -> DeviceOutcome:
        assert code == "device-secret"
        return DeviceOutcome(
            "authorized",
            IssuedAgentToken(uuid4(), "acr_agent-only", datetime(2026, 12, 5, tzinfo=UTC)),
        )


def test_public_agent_instructions_describe_complete_sequence() -> None:
    response = TestClient(app).get("/api/agent/instructions")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    for operation in (
        "/api/agent/device/start",
        "/api/agent/device/poll",
        "/api/repos",
        "/api/runs",
        ".review/rules.md",
    ):
        assert operation in response.text
    assert "HTTP 502 or another server error" in response.text
    assert "restart from step 2" in response.text

    review_events = re.search(r"`reviewEvent` \(([^)]+)\)", response.text)
    assert review_events is not None
    values = re.findall(r"`([^`]+)`", review_events.group(1))
    assert values
    for value in values:
        assert RepositoryUpdateDto.model_validate({"reviewEvent": value}).review_event == value


def test_device_endpoints_return_only_agent_token() -> None:
    app.dependency_overrides[get_agent_device_access] = lambda: FakeDeviceAccess()
    try:
        client = TestClient(app)
        challenge = client.post("/api/agent/device/start", json={"repository": "octo/repo"})
        assert challenge.status_code == 200
        assert challenge.json()["userCode"] == "ABCD-EFGH"
        result = client.post(
            "/api/agent/device/poll", json={"deviceCode": challenge.json()["deviceCode"]}
        )
        assert result.status_code == 200
        assert result.json()["status"] == "authorized"
        assert result.json()["token"] == "acr_agent-only"
        assert "ghu_" not in result.text
    finally:
        app.dependency_overrides.clear()


class FakeTokens:
    async def revoke(self, user_id: int, token_id: object) -> bool:
        assert user_id == 42
        return True


def test_token_revoke_requires_owner_bearer() -> None:
    app.dependency_overrides[get_agent_tokens] = lambda: FakeTokens()
    try:
        response = TestClient(app).delete(f"/api/agent/tokens/{uuid4()}")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_owner_can_revoke_but_agent_cannot_revoke_other_token() -> None:
    token_id = uuid4()
    app.dependency_overrides[get_agent_tokens] = lambda: FakeTokens()
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, ())
    try:
        assert TestClient(app).delete(f"/api/agent/tokens/{token_id}").status_code == 204
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(
            42, (), agent_repository_id=uuid4()
        )
        assert TestClient(app).delete(f"/api/agent/tokens/{token_id}").status_code == 403
    finally:
        app.dependency_overrides.clear()
