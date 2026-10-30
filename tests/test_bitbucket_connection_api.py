"""The Bitbucket browser surface is authenticated and workspace scoped (#333)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace, TracebackType
from typing import Self, cast
from uuid import UUID

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.bootstrap import bitbucket as bitbucket_bootstrap
from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.api.connections import (
    ConnectionItem,
    ConnectResponse,
    DiscoveredRepository,
)
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketCredentialCodec,
    BitbucketManagementStore,
    BitbucketManagementUnitOfWork,
    BitbucketRepositoryProvider,
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.bitbucket.infrastructure.credential_codec import (
    BitbucketCredentialCodecAdapter,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
    BitbucketProviderError,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000333")
REPOSITORY = UUID("00000000-0000-4000-8000-000000000334")


@pytest.mark.parametrize("response_model", [ConnectResponse, ConnectionItem])
@pytest.mark.parametrize("field", ["workspace_slug", "repository_slug"])
def test_connected_repository_response_slugs_stop_at_128_characters(
    response_model: type[ConnectResponse] | type[ConnectionItem], field: str
) -> None:
    values: dict[str, object] = {
        "id": UUID(int=333),
        "repository_id": REPOSITORY,
        "repository_uuid": UUID(int=335),
        "workspace_slug": "team",
        "repository_slug": "repo",
        "status": "active",
        "credential_generation": 1,
    }
    if response_model is ConnectResponse:
        values["webhook"] = None
    else:
        values["webhook_url"] = "https://portal.test/webhooks/bitbucket"

    values[field] = "a" * 128
    assert getattr(response_model.model_validate(values), field) == "a" * 128
    values[field] = "a" * 129
    with pytest.raises(ValidationError):
        response_model.model_validate(values)


def test_connected_slug_bounds_are_in_http_schema_without_bounding_discovery() -> None:
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    for name in ("ConnectResponse", "ConnectionItem"):
        for field in ("workspaceSlug", "repositorySlug"):
            assert (
                schemas[name]["properties"][field]["minLength"],
                schemas[name]["properties"][field]["maxLength"],
            ) == (1, 128)
    for field in ("workspaceSlug", "slug"):
        assert "maxLength" not in schemas["DiscoveredRepository"]["properties"][field]
    for field in ("workspaceSlug", "slug", "name", "htmlUrl"):
        assert schemas["DiscoveredRepository"]["properties"][field]["minLength"] == 1
    assert schemas["WebhookSetup"]["properties"]["events"]["items"]["enum"] == [
        "repo:push",
        "pullrequest:created",
        "pullrequest:updated",
        "pullrequest:fulfilled",
        "pullrequest:rejected",
    ]
    assert schemas["ConnectionList"]["properties"]["nextCursor"]["type"] == "null"
    for field in ("bitbucketWorkspace", "repositorySlug"):
        assert schemas["ConnectRequest"]["properties"][field]["maxLength"] == 128

    discovered = DiscoveredRepository(
        uuid=REPOSITORY,
        workspace_slug="w" * 129,
        slug="r" * 129,
        name="Provider repo",
        is_private=True,
        html_url="https://bitbucket.org/team/repo",
    )
    assert len(discovered.workspace_slug) == len(discovered.slug) == 129


class _MembershipOnlyStore:
    def __init__(self, member: bool) -> None:
        self.member = member

    async def is_member(self, *, user_id: int, workspace_id: UUID, lock: bool = False) -> bool:
        assert user_id == 42
        assert workspace_id == WORKSPACE
        return self.member

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"management passed membership fence: {name}")


class _MembershipOnlyUow:
    def __init__(self, member: bool) -> None:
        self._store = _MembershipOnlyStore(member)

    @property
    def connections(self) -> BitbucketManagementStore:
        return cast(BitbucketManagementStore, self._store)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        raise AssertionError("management passed membership fence: commit")

    async def rollback(self) -> None:
        raise AssertionError("management passed membership fence: rollback")


class _AbsentRepositoryStore(_MembershipOnlyStore):
    def __init__(self) -> None:
        super().__init__(member=True)
        self.disconnect_calls = 0

    async def disconnect_repository(
        self, *, workspace_id: UUID, installation_id: UUID, repository_id: UUID
    ) -> bool:
        assert (workspace_id, installation_id, repository_id) == (
            WORKSPACE,
            REPOSITORY,
            UUID(int=335),
        )
        self.disconnect_calls += 1
        return False


class _AbsentRepositoryUow(_MembershipOnlyUow):
    def __init__(self) -> None:
        super().__init__(member=True)
        self._store = _AbsentRepositoryStore()
        self.commits = 0

    @property
    def store(self) -> _AbsentRepositoryStore:
        return cast(_AbsentRepositoryStore, self._store)

    async def commit(self) -> None:
        self.commits += 1


class _MissingManagementStore(_MembershipOnlyStore):
    def __init__(self) -> None:
        super().__init__(member=True)
        self.connection_lookups = 0
        self.secret_rotations = 0

    async def installation_by_id(self, *, workspace_id: UUID, installation_id: UUID) -> None:
        assert (workspace_id, installation_id) == (WORKSPACE, REPOSITORY)
        self.connection_lookups += 1
        return None

    async def list_bindings(self, *, workspace_id: UUID) -> tuple[()]:
        assert workspace_id == WORKSPACE
        return ()

    async def set_repository_webhook_secret(
        self, *, workspace_id: UUID, installation_id: UUID, repository_id: UUID, secret: object
    ) -> int:
        assert (workspace_id, installation_id, repository_id) == (
            WORKSPACE,
            REPOSITORY,
            UUID(int=335),
        )
        self.secret_rotations += 1
        raise ValueError("repository_not_found")


class _MissingManagementUow(_MembershipOnlyUow):
    def __init__(self) -> None:
        super().__init__(member=True)
        self._store = _MissingManagementStore()

    @property
    def store(self) -> _MissingManagementStore:
        return cast(_MissingManagementStore, self._store)


def _manager_with_membership(
    *, member: bool, provider: object | None = None
) -> ManageBitbucketConnection:
    return ManageBitbucketConnection(
        uow_factory=lambda: cast(BitbucketManagementUnitOfWork, _MembershipOnlyUow(member)),
        cipher=cast(BitbucketCredentialCodec, object()),
        provider=cast(BitbucketRepositoryProvider, provider or object()),
        rule_sets={},
        webhook_base_url="https://portal.example.test",
    )


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
        (
            "post",
            f"connections/{REPOSITORY}/repositories/{UUID(int=335)}/webhook-secret",
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


def test_connect_rejects_repository_uuid_mismatch_before_probing_or_persisting() -> None:
    fetched: list[tuple[str, str]] = []

    class MismatchedRepositoryProvider:
        async def fetch_repository(
            self, *, workspace_slug: str, repository_slug: str, token: str
        ) -> SimpleNamespace:
            assert token == "test-token"
            fetched.append((workspace_slug, repository_slug))
            return SimpleNamespace(uuid=UUID(int=999), workspace_uuid=UUID(int=998))

        async def probe_pull_request_read(self, **_kwargs: object) -> None:
            raise AssertionError("mismatched repository reached provider probe")

    manager = _manager_with_membership(member=True, provider=MismatchedRepositoryProvider())
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections",
            json={
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(REPOSITORY),
                "apiToken": "test-token",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json() == {"detail": "repository_uuid_mismatch"}
    assert fetched == [("team", "repo")]


def test_connect_rejects_token_without_user_scope_before_persisting() -> None:
    class MissingUserScopeProvider:
        async def fetch_repository(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(uuid=REPOSITORY, workspace_uuid=UUID(int=998))

        async def probe_pull_request_read(self, **_kwargs: object) -> None:
            return None

        async def probe_current_user(self, **_kwargs: object) -> None:
            raise BitbucketProviderError("repository_inaccessible")

    manager = _manager_with_membership(member=True, provider=MissingUserScopeProvider())
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections",
            json={
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(REPOSITORY),
                "apiToken": "test-token",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json() == {"detail": "repository_inaccessible"}


@pytest.mark.parametrize("code", ["invalid_slug", "invalid_credential"])
def test_provider_rejection_has_explicit_http_code_and_sanitized_body(code: str) -> None:
    class RejectingProvider:
        async def fetch_repository(self, **_kwargs: object) -> None:
            raise BitbucketProviderError(code)

    manager = _manager_with_membership(member=True, provider=RejectingProvider())
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        response = TestClient(app).post(
            f"/api/workspaces/{WORKSPACE}/bitbucket/connections",
            json={
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(REPOSITORY),
                "apiToken": "test-token",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {"detail": code}
    assert "test-token" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("delete", f"connections/{REPOSITORY}/repositories/{UUID(int=335)}"),
        (
            "post",
            f"connections/{REPOSITORY}/repositories/{UUID(int=335)}/webhook-secret",
        ),
    ],
)
def test_removed_member_cannot_disconnect_or_rotate_webhook_secret(method: str, path: str) -> None:
    # The claim still names the workspace, but database membership was removed.
    manager = _manager_with_membership(member=False)
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        response = TestClient(app).request(method, f"/api/workspaces/{WORKSPACE}/bitbucket/{path}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "workspace_not_found"}


def test_valid_member_can_repeat_disconnect_of_absent_repository() -> None:
    uow = _AbsentRepositoryUow()
    manager = ManageBitbucketConnection(
        uow_factory=lambda: cast(BitbucketManagementUnitOfWork, uow),
        cipher=cast(BitbucketCredentialCodec, object()),
        provider=cast(BitbucketRepositoryProvider, object()),
        rule_sets={},
        webhook_base_url="https://portal.example.test",
    )
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        client = TestClient(app)
        responses = [
            client.delete(
                f"/api/workspaces/{WORKSPACE}/bitbucket/connections/{REPOSITORY}"
                f"/repositories/{UUID(int=335)}"
            )
            for _ in range(2)
        ]
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in responses] == [204, 204]
    assert uow.store.disconnect_calls == 2
    assert uow.commits == 2


@pytest.mark.parametrize(
    ("method", "path", "body", "code"),
    [
        (
            "put",
            f"connections/{REPOSITORY}/credential",
            {"apiToken": "test-token"},
            "connection_not_found",
        ),
        (
            "post",
            f"connections/{REPOSITORY}/repositories/{UUID(int=335)}/webhook-secret",
            None,
            "repository_not_found",
        ),
    ],
)
def test_connection_and_repository_not_found_codes_are_explicit_on_wire(
    method: str, path: str, body: dict[str, str] | None, code: str
) -> None:
    uow = _MissingManagementUow()
    manager = ManageBitbucketConnection(
        uow_factory=lambda: cast(BitbucketManagementUnitOfWork, uow),
        cipher=BitbucketCredentialCodecAdapter(
            BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
        ),
        provider=cast(BitbucketRepositoryProvider, object()),
        rule_sets={},
        webhook_base_url="https://portal.example.test",
    )

    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        response = TestClient(app).request(
            method, f"/api/workspaces/{WORKSPACE}/bitbucket/{path}", json=body
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": code}
    assert (uow.store.connection_lookups, uow.store.secret_rotations) == (
        (1, 0) if method == "put" else (0, 1)
    )


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
