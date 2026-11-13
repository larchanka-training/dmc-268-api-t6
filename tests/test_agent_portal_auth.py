"""Agent bearer is confined to repository configuration and review reads."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
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
        assert client.get("/api/agent/tokens", headers=headers).status_code == 403
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


def test_agent_route_allowlist_contains_only_repository_settings_and_review_reads() -> None:
    assert {
        ("GET", "/api/repos"),
        ("GET", "/api/repos/{repo_id}"),
        ("PATCH", "/api/repos/{repo_id}"),
        ("GET", "/api/repos/{repo_id}/pulls"),
        ("GET", "/api/repos/{repo_id}/rules-status"),
        ("GET", "/api/runs"),
        ("GET", "/api/runs/{run_id}"),
        ("GET", "/api/runs/{run_id}/comments"),
        ("GET", "/api/runs/{run_id}/actions"),
    } == portal_auth._AGENT_OPERATIONS


def test_agent_allowlist_matches_live_routes_and_rejects_other_protected_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portal_auth, "_agent_tokens_for_request", lambda request: FakeAgentTokens())
    routes = [
        nested
        for entry in app.routes
        for nested in getattr(getattr(entry, "original_router", None), "routes", [entry])
        if isinstance(nested, APIRoute)
    ]
    existing = {(method, route.path) for route in routes for method in route.methods or ()}
    assert existing >= portal_auth._AGENT_OPERATIONS

    def requires_auth(dependency: object) -> bool:
        if getattr(dependency, "call", None) is portal_auth.get_auth_scope:
            return True
        return any(requires_auth(item) for item in getattr(dependency, "dependencies", ()))

    async def exercise() -> None:
        for route in routes:
            if not requires_auth(route.dependant):
                continue
            for method in route.methods or ():
                request = Request(
                    {
                        "type": "http",
                        "method": method,
                        "path": route.path,
                        "headers": [(b"authorization", b"Bearer acr_test")],
                        "route": route,
                        "app": app,
                    }
                )
                if (method, route.path) in portal_auth._AGENT_OPERATIONS:
                    scope = await portal_auth.get_auth_scope(request)
                    assert scope.agent_repository_id is not None
                else:
                    with pytest.raises(HTTPException) as error:
                        await portal_auth.get_auth_scope(request)
                    assert error.value.status_code == 403

    asyncio.run(exercise())
