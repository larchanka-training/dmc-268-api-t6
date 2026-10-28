"""The Bitbucket browser surface is authenticated and workspace scoped (#333)."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketProviderError,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000333")
INSTALLATION = UUID("00000000-0000-4000-8000-000000000334")
REPOSITORY = UUID("00000000-0000-4000-8000-000000000335")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param(
            "post",
            f"/api/workspaces/{WORKSPACE}/bitbucket/discovery",
            {"bitbucketWorkspace": "team"},
            id="discovery",
        ),
        pytest.param(
            "post",
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(REPOSITORY),
            },
            id="connect",
        ),
        pytest.param(
            "put",
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections/{INSTALLATION}/credential",
            {},
            id="rotate",
        ),
    ],
)
@pytest.mark.parametrize(
    ("token", "expected_status"),
    [
        pytest.param("A" * 4097, 422, id="too-long"),
        pytest.param("A token", 422, id="space"),
        pytest.param("A\n", 422, id="trailing-newline"),
        pytest.param("A\x7f", 422, id="delete"),
        pytest.param("Aé", 422, id="non-ascii"),
        pytest.param("A" * 4096, 502, id="maximum-length-accepted"),
    ],
)
def test_bitbucket_api_token_rejected_before_manager_and_never_echoed(
    method: str, path: str, body: dict[str, str], token: str, expected_status: int
) -> None:
    calls: list[str] = []

    class RecordingManager:
        async def discover(self, **_kwargs: object) -> None:
            calls.append("discover")
            raise BitbucketProviderError("provider_unavailable")

        async def connect(self, **_kwargs: object) -> None:
            calls.append("connect")
            raise BitbucketProviderError("provider_unavailable")

        async def rotate_token(self, **_kwargs: object) -> None:
            calls.append("rotate")
            raise BitbucketProviderError("provider_unavailable")

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = RecordingManager
    try:
        response = TestClient(app).request(method, path, json={**body, "apiToken": token})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == expected_status
    expected_call = {
        "discovery": "discover",
        "connections": "connect",
        "credential": "rotate",
    }[path.rsplit("/", 1)[-1]]
    assert calls == ([] if expected_status == 422 else [expected_call])
    if expected_status == 422:
        assert token not in response.text
        assert all(
            "input" not in error and "ctx" not in error for error in response.json()["detail"]
        )


def test_bitbucket_connection_and_rotation_return_all_webhook_events() -> None:
    from app.modules.integrations.bitbucket.application.manage_connection import (
        ConnectResult,
        WebhookSecretResult,
    )

    class RecordingManager:
        async def connect(self, **_kwargs: object) -> ConnectResult:
            return ConnectResult(
                INSTALLATION, REPOSITORY, REPOSITORY, "team", "repo", 1, "hook-secret"
            )

        async def rotate_webhook_secret(self, **_kwargs: object) -> WebhookSecretResult:
            return WebhookSecretResult("https://example.test/hook", "new-hook-secret")

        def webhook_url(self, _installation_id: UUID, _repository_id: UUID) -> str:
            return "https://example.test/hook"

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = RecordingManager
    try:
        client = TestClient(app)
        connected = client.post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections",
            json={
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(REPOSITORY),
                "apiToken": "valid-token",
            },
        )
        rotated = client.post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections/{INSTALLATION}/repositories/{REPOSITORY}/webhook-secret"
        )
    finally:
        app.dependency_overrides.clear()

    expected = [
        "repo:push",
        "pullrequest:created",
        "pullrequest:updated",
        "pullrequest:fulfilled",
        "pullrequest:rejected",
    ]
    assert connected.status_code == 201
    assert rotated.status_code == 200
    assert connected.json()["webhook"]["events"] == expected
    assert rotated.json()["events"] == expected


def test_bitbucket_validation_escapes_surrogates_and_hides_unknown_field_names() -> None:
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = object
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/discovery",
            content=json.dumps(
                {
                    "bitbucketWorkspace": "team",
                    "apiToken": "private token marker",
                    "private-key-marker": "private-value-marker",
                    "\ud800": "surrogate-value-marker",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        unknown_field = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/discovery",
            json={
                "bitbucketWorkspace": "team",
                "apiToken": "private token marker",
                "private-key-marker": "private-value-marker",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "private token marker" not in response.text
    assert "private-key-marker" not in response.text
    assert "private-value-marker" not in response.text
    assert "surrogate-value-marker" not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])
    assert unknown_field.status_code == 422
    assert "private-key-marker" not in unknown_field.text
    assert "private-value-marker" not in unknown_field.text


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
