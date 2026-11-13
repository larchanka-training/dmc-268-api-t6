"""Public device bootstrap and bearer-only token revocation contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.agent_access import get_agent_device_access
from app.bootstrap.portal_auth import get_agent_tokens, get_auth_scope
from app.main import app
from app.modules.auth.application.agent_device_access import (
    DeviceChallenge,
    DeviceOutcome,
    DevicePollTooSoon,
)
from app.modules.auth.application.agent_tokens import (
    AgentTokenRevocationForbidden,
    IssuedAgentToken,
    OwnedAgentToken,
    RepositoryNotAvailable,
)
from app.modules.auth.application.exchange_github_code import GitHubProviderUnavailable
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
        "/api/agent/tokens",
        "/api/repos",
        "/api/runs",
        ".review/rules.md",
    ):
        assert operation in response.text
    assert "HTTP 502 or another server error" in response.text
    assert "restart from step 2" in response.text
    assert "add the `ai-review` label" in response.text
    assert "`status=present` and `sha` equals the commit" in response.text
    assert "If the user requested no rules change" in response.text
    assert "`missing` means the service defaults apply" in response.text
    assert "opens **Agent tokens** from the AI Code Reviewer user menu" in response.text
    assert "remove and reapply the label" in response.text
    assert "If no run appears" in response.text

    review_events = re.search(r"`reviewEvent` \(([^)]+)\)", response.text)
    assert review_events is not None
    values = re.findall(r"`([^`]+)`", review_events.group(1))
    assert values == ["COMMENT", "REQUEST_CHANGES"]
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


@pytest.mark.parametrize(
    ("error", "status", "retry_after"),
    [
        (DevicePollTooSoon(9), 429, "9"),
        (RepositoryNotAvailable(), 404, None),
        (GitHubProviderUnavailable(), 502, None),
    ],
)
def test_device_poll_maps_expected_failures(
    error: Exception, status: int, retry_after: str | None
) -> None:
    class FailingDeviceAccess(FakeDeviceAccess):
        async def poll(self, code: str) -> DeviceOutcome:
            raise error

    app.dependency_overrides[get_agent_device_access] = lambda: FailingDeviceAccess()
    try:
        response = TestClient(app).post(
            "/api/agent/device/poll", json={"deviceCode": "device-secret"}
        )
        assert response.status_code == status
        assert response.headers.get("Retry-After") == retry_after
    finally:
        app.dependency_overrides.clear()


def test_device_start_maps_github_provider_failure() -> None:
    class FailingDeviceAccess(FakeDeviceAccess):
        async def start(self, repository: str) -> DeviceChallenge:
            raise GitHubProviderUnavailable

    app.dependency_overrides[get_agent_device_access] = lambda: FailingDeviceAccess()
    try:
        response = TestClient(app).post("/api/agent/device/start", json={"repository": "octo/repo"})
        assert response.status_code == 502
    finally:
        app.dependency_overrides.clear()


def test_device_start_requires_configured_github_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    response = TestClient(app).post("/api/agent/device/start", json={"repository": "octo/repo"})
    assert response.status_code == 503


class FakeTokens:
    async def list_active(self, scope: AuthScope) -> list[OwnedAgentToken]:
        assert scope.user_id == 42
        return [
            OwnedAgentToken(
                uuid4(),
                "octo/repo",
                datetime(2026, 11, 5, tzinfo=UTC),
                datetime(2026, 12, 5, tzinfo=UTC),
            )
        ]

    async def revoke(self, scope: AuthScope, token_id: object) -> bool:
        assert scope.user_id == 42
        if scope.agent_repository_id is not None:
            raise AgentTokenRevocationForbidden
        return True


def test_token_revoke_requires_owner_bearer() -> None:
    app.dependency_overrides[get_agent_tokens] = lambda: FakeTokens()
    try:
        response = TestClient(app).delete(f"/api/agent/tokens/{uuid4()}")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_owner_can_list_token_ids_without_credentials() -> None:
    app.dependency_overrides[get_agent_tokens] = lambda: FakeTokens()
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, ())
    try:
        response = TestClient(app).get("/api/agent/tokens")
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["repository"] == "octo/repo"
        assert "id" in response.json()[0]
        assert "secret" not in response.text
        assert "tokenHash" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_token_listing_requires_configured_agent_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "reviews_api_resources", None, raising=False)
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, ())
    try:
        assert TestClient(app).get("/api/agent/tokens").status_code == 503
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
