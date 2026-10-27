"""The Bitbucket browser surface is authenticated and workspace scoped (#333)."""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketProviderError,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000333")


def test_bitbucket_connection_list_requires_portal_authentication() -> None:
    response = TestClient(app).get(f"/api/workspaces/{WORKSPACE}/bitbucket/connections")

    assert response.status_code == 401


def test_bitbucket_connection_list_rejects_workspace_outside_claim() -> None:
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, ())
    try:
        response = TestClient(app).get(f"/api/workspaces/{WORKSPACE}/bitbucket/connections")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_discovery_propagates_sanitized_retry_after() -> None:
    class LimitedManager:
        async def discover(self, **_kwargs: object) -> None:
            raise BitbucketProviderError("provider_rate_limited", retry_after=37)

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: LimitedManager()
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/discovery",
            json={"bitbucketWorkspace": "nvdovnar", "apiToken": "secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "37"
    assert response.json()["detail"] == "provider_rate_limited"


def test_bitbucket_connection_list_shows_per_repository_webhook_confirmation() -> None:
    from datetime import UTC, datetime

    from app.modules.integrations.bitbucket.application.manage_connection import RepositoryBinding

    installation, first, second = UUID(int=333), UUID(int=334), UUID(int=335)

    class ListedManager:
        async def list_connections(self, **_kwargs: object) -> tuple[RepositoryBinding, ...]:
            return (
                RepositoryBinding(
                    installation,
                    first,
                    UUID(int=336),
                    "team/first",
                    "https://bitbucket.org/team/first",
                    1,
                    1,
                    datetime.now(UTC),
                ),
                RepositoryBinding(
                    installation,
                    second,
                    UUID(int=337),
                    "team/second",
                    "https://bitbucket.org/team/second",
                    1,
                    1,
                    None,
                ),
            )

        def webhook_url(self, installation_id: UUID, repository_id: UUID) -> str:
            return f"https://example.test/webhooks/bitbucket/{installation_id}/{repository_id}"

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: ListedManager()
    try:
        response = TestClient(app).get(f"/api/workspaces/{WORKSPACE}/bitbucket/connections")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert {item["repositoryId"]: item["status"] for item in response.json()["connections"]} == {
        str(first): "active",
        str(second): "awaiting_webhook",
    }
