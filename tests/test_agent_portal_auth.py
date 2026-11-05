"""Agent bearer is confined to repository configuration and review reads."""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import portal_auth
from app.main import app, get_run_repository
from app.modules.auth.application.scope import AuthScope


class FakeAgentTokens:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    async def resolve(self, token: str) -> AuthScope | None:
        if self.valid and token == "acr_test":
            return AuthScope(
                42,
                (UUID("00000000-0000-0000-0000-000000000001"),),
                agent_repository_id=UUID("00000000-0000-0000-0000-000000000002"),
            )
        return None


class EmptyRuns:
    async def list_runs(self, **kwargs: object) -> list[object]:
        return []


def test_agent_token_can_read_runs_but_cannot_use_unrelated_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portal_auth, "_agent_tokens_for_request", lambda request: FakeAgentTokens())
    app.dependency_overrides[get_run_repository] = lambda: EmptyRuns()
    try:
        client = TestClient(app)
        headers = {"Authorization": "Bearer acr_test"}
        assert client.get("/api/runs", headers=headers).status_code == 200
        assert client.get("/api/metrics/summary", headers=headers).status_code == 403
        assert client.get("/api/auth/me", headers=headers).status_code == 403
        assert (
            client.post(
                "/api/runs/00000000-0000-0000-0000-000000000003/cancel", headers=headers
            ).status_code
            == 403
        )
    finally:
        app.dependency_overrides.clear()


def test_revoked_agent_token_is_rejected_before_portal_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        portal_auth, "_agent_tokens_for_request", lambda request: FakeAgentTokens(valid=False)
    )
    assert (
        TestClient(app).get("/api/runs", headers={"Authorization": "Bearer acr_test"}).status_code
        == 401
    )
