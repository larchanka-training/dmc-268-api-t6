"""The Bitbucket browser surface is authenticated and workspace scoped (#333)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.bootstrap import bitbucket as bitbucket_bootstrap
from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
    BitbucketProviderError,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000333")
REPOSITORY = UUID("00000000-0000-4000-8000-000000000334")


@pytest.mark.parametrize(
    ("path", "method", "body"),
    [
        ("discovery", "post", {"bitbucketWorkspace": "team"}),
        (
            "connections",
            "post",
            {
                "bitbucketWorkspace": "team",
                "repositoryUuid": str(REPOSITORY),
            },
        ),
        (f"connections/{REPOSITORY}/credential", "put", {}),
    ],
)
def test_validation_response_never_echoes_credential(
    path: str, method: str, body: dict[str, str]
) -> None:
    class UnexpectedManager:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("validation must happen before provider access")

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: UnexpectedManager()
    try:
        response = TestClient(app).request(
            method,
            f"/api/workspaces/{WORKSPACE}/bitbucket/{path}",
            json={**body, "apiToken": "SECRET-TOKEN-123", "unexpected": "SECRET-TOKEN-123"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "SECRET-TOKEN-123" not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])


def test_discovery_extra_field_name_cannot_echo_credential_in_validation_location() -> None:
    class UnexpectedManager:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("validation must happen before provider access")

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: UnexpectedManager()
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/discovery",
            json={
                "bitbucketWorkspace": "team",
                "apiToken": "canary-token",
                "SECRET-TOKEN-123": "anything",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {"type": "extra_forbidden", "loc": ["body", "<unknown field>"], "msg": "Invalid input"}
        ]
    }
    assert "SECRET-TOKEN-123" not in response.text


@pytest.mark.parametrize("token", ["", "secret ", "secret\n", "secret\u00a0", "x" * 4097])
@pytest.mark.parametrize(
    ("path", "method", "body"),
    [
        ("discovery", "post", {"bitbucketWorkspace": "team"}),
        (
            "connections",
            "post",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(REPOSITORY),
            },
        ),
        (f"connections/{REPOSITORY}/credential", "put", {}),
    ],
)
def test_invalid_token_is_rejected_before_manager(
    path: str, method: str, body: dict[str, str], token: str
) -> None:
    class UnexpectedManager:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("invalid token reached manager")

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: UnexpectedManager()
    try:
        response = TestClient(app).request(
            method,
            f"/api/workspaces/{WORKSPACE}/bitbucket/{path}",
            json={**body, "apiToken": token},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert token not in response.text if token else True


@pytest.mark.parametrize("slug", ["", "bad slug", "bad/slug", "a" * 129])
@pytest.mark.parametrize("field", ["bitbucketWorkspace", "repositorySlug"])
def test_invalid_slug_is_rejected_before_manager(slug: str, field: str) -> None:
    class UnexpectedManager:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("invalid slug reached manager")

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: UnexpectedManager()
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections",
            json={
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(REPOSITORY),
                "apiToken": "secret",
                field: slug,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_discovery_normalizes_workspace_slug_before_manager() -> None:
    received: list[str] = []

    class Manager:
        async def discover(self, **kwargs: object) -> SimpleNamespace:
            received.append(str(kwargs["workspace_slug"]))
            return SimpleNamespace(repositories=(), next_cursor=None)

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: Manager()
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/discovery",
            json={"bitbucketWorkspace": "TEAM", "apiToken": "secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert received == ["team"]


def test_composed_management_client_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("NO_PROXY", "")

    def unexpected_environment_proxy_lookup() -> dict[str, str]:
        raise AssertionError("credential client consulted environment proxy settings")

    monkeypatch.setattr(
        "httpx._client.get_environment_proxies", unexpected_environment_proxy_lookup
    )
    with pytest.raises(AssertionError, match="consulted environment proxy"):
        httpx.AsyncClient(trust_env=True)
    monkeypatch.setattr(
        bitbucket_bootstrap,
        "_cipher_from_environment",
        lambda: BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32}),
    )
    monkeypatch.setattr(
        bitbucket_bootstrap, "_webhook_base_from_environment", lambda: "https://portal.test"
    )
    resources = ReviewsApiResources.__new__(ReviewsApiResources)
    monkeypatch.setattr(resources, "_session_factory", object(), raising=False)
    monkeypatch.setattr(app.state, "reviews_api_resources", resources, raising=False)
    providers: list[BitbucketCloudRepositoryProvider] = []

    class CapturingManager:
        def __init__(self, **kwargs: object) -> None:
            provider = kwargs["provider"]
            assert isinstance(provider, BitbucketCloudRepositoryProvider)
            providers.append(provider)

    monkeypatch.setattr(bitbucket_bootstrap, "ManageBitbucketConnection", CapturingManager)

    async def scenario() -> None:
        request = Request({"type": "http", "app": app})
        async for _manager in get_bitbucket_management(request, AuthScope(42, (WORKSPACE,))):
            assert len(providers) == 1

    asyncio.run(scenario())


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


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "connections", None),
        (
            "put",
            f"connections/{REPOSITORY}/credential",
            {"apiToken": "new-secret"},
        ),
        (
            "delete",
            f"connections/{REPOSITORY}/repositories/{UUID(int=335)}",
            None,
        ),
    ],
)
def test_other_workspace_cannot_list_rotate_or_disconnect(
    method: str, path: str, body: dict[str, str] | None
) -> None:
    class UnexpectedManager:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("unclaimed workspace reached connection manager")

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, ())
    app.dependency_overrides[get_bitbucket_management] = lambda: UnexpectedManager()
    try:
        response = TestClient(app).request(
            method, f"/api/workspaces/{WORKSPACE}/bitbucket/{path}", json=body
        )
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
