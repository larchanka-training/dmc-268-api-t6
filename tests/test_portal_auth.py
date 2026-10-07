"""Bearer boundary for the portal API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app.bootstrap.auth_api import get_current_user
from app.main import app, get_run_repository
from app.modules.auth.application.get_me import CurrentUser, GetCurrentUser, MyWorkspace
from app.modules.auth.application.scope import AuthScope


class EmptyRuns:
    async def list_runs(self, **kwargs: object) -> list[object]:
        return []


def _keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )


def _token(private_key: str, **overrides: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": "42",
        "github_user_id": 42,
        "workspaces": [str(UUID("00000000-0000-0000-0000-000000000001"))],
        "iss": "review-api",
        "aud": "review-ui",
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _portal_routes() -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    dummy_uuid = "00000000-0000-0000-0000-000000000001"
    # Enumerate the composed app, including auth_router and hidden/direct app routes.
    # These three endpoints authenticate via an OAuth code or refresh cookie.
    public_auth = {
        ("POST", "/api/auth/github/callback"),
        ("POST", "/api/auth/refresh"),
        ("POST", "/api/auth/logout"),
    }
    for route in iter_route_contexts(app.routes):
        if route.path is None or not route.path.startswith("/api/"):
            continue
        path = (
            route.path.replace("{run_id}", dummy_uuid)
            .replace("{repo_id}", dummy_uuid)
            .replace("{index}", "0")
        )
        if not route.methods:
            continue
        for method in sorted(route.methods):
            if (method, route.path) not in public_auth:
                routes.append((method, path))
    return routes


@pytest.mark.parametrize(
    ("method", "path"),
    _portal_routes(),
)
def test_every_portal_route_rejects_missing_bearer(method: str, path: str) -> None:
    assert TestClient(app).request(method, path).status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"workspaces": "not-an-array"},
        {"workspaces": ["not-a-uuid"]},
        {"workspaces": [None]},
        {"sub": "someone"},
        {"github_user_id": 99},
        {"github_user_id": 42.0},
        {"exp": float((datetime.now(UTC) + timedelta(minutes=15)).timestamp())},
        {"iss": "wrong"},
        {"aud": "wrong"},
        {"exp": 1},
    ],
)
def test_portal_rejects_invalid_claims(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, object]
) -> None:
    private, public = _keys()
    monkeypatch.setenv("AUTH_JWT_PUBLIC_KEY", public)
    monkeypatch.setenv("AUTH_JWT_ISSUER", "review-api")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "review-ui")
    response = TestClient(app).get(
        "/api/runs", headers={"Authorization": f"Bearer {_token(private, **claims)}"}
    )
    assert response.status_code == 401


def test_portal_rejects_missing_required_claims_and_wrong_signature_or_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _keys()
    other_private, _ = _keys()
    monkeypatch.setenv("AUTH_JWT_PUBLIC_KEY", public)
    monkeypatch.setenv("AUTH_JWT_ISSUER", "review-api")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "review-ui")
    original_claims = jwt.decode(_token(private), options={"verify_signature": False})
    missing_tokens = []
    for required in ("workspaces", "exp", "iss", "aud", "sub"):
        claims = original_claims.copy()
        del claims[required]
        missing_tokens.append(jwt.encode(claims, private, algorithm="RS256"))
    wrong_algorithm = jwt.encode(
        original_claims, "shared-secret-for-test-only-32-bytes", algorithm="HS256"
    )
    for token in (*missing_tokens, _token(other_private), wrong_algorithm):
        assert (
            TestClient(app)
            .get("/api/runs", headers={"Authorization": f"Bearer {token}"})
            .status_code
            == 401
        )


def test_portal_list_requires_strict_bearer_and_accepts_empty_workspace_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _keys()
    monkeypatch.setenv("AUTH_JWT_PUBLIC_KEY", public)
    monkeypatch.setenv("AUTH_JWT_ISSUER", "review-api")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "review-ui")
    app.dependency_overrides[get_run_repository] = lambda: EmptyRuns()
    try:
        client = TestClient(app)
        assert client.get("/api/runs").status_code == 401
        assert client.get("/api/runs", headers={"Authorization": "Basic abc"}).status_code == 401
        valid = client.get(
            "/api/runs",
            headers={"Authorization": f"Bearer {_token(private, workspaces=[])}"},
        )
        assert valid.status_code == 200
        assert valid.json() == {"items": [], "nextCursor": None}
    finally:
        app.dependency_overrides.clear()


# Empty workspace repository visibility is covered against real SQL by
# test_portal_scope_postgres.py:
# test_portal_routes_intersect_claim_current_membership_and_repository_grant.
# An always-empty fake here cannot verify that authorization predicate.


def test_me_requires_bearer_and_returns_current_profile_with_claimed_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _keys()
    monkeypatch.setenv("AUTH_JWT_PUBLIC_KEY", public)
    monkeypatch.setenv("AUTH_JWT_ISSUER", "review-api")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "review-ui")

    class CurrentUserPort:
        async def get_current_user(self, scope: AuthScope) -> CurrentUser:
            assert scope.user_id == 42
            assert scope.workspace_ids == (UUID("00000000-0000-0000-0000-000000000001"),)
            return CurrentUser(
                42,
                "octocat",
                "Octo Cat",
                None,
                (MyWorkspace(UUID("00000000-0000-0000-0000-000000000001"), "Alpha", 17),),
            )

    app.dependency_overrides[get_current_user] = lambda: GetCurrentUser(CurrentUserPort())
    try:
        client = TestClient(app)
        assert client.get("/api/auth/me").status_code == 401
        response = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {_token(private)}"}
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {
        "id": 42,
        "login": "octocat",
        "name": "Octo Cat",
        "avatarUrl": None,
        "workspaces": [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "name": "Alpha",
                "installationId": 17,
            }
        ],
    }
