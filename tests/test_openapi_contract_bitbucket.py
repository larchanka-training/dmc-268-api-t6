"""The Bitbucket operations of the browser API against ``contracts/openapi.yaml``."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import ValidationError as PydanticValidationError

from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.main import (
    app,
)
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.api.connections import ConnectionItem, ConnectResponse
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketManagementError,
    ConnectionPage,
    ConnectResult,
    RepositoryBinding,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketProviderError,
)
from tests.openapi_contract import _components, _spec, _validator_at, _validator_for
from tests.test_ui_zod_contracts import (
    _generated_schemas,
)


@pytest.mark.parametrize(
    ("schema_name", "body"),
    [
        ("BitbucketDiscoveryRequest", {"bitbucketWorkspace": "team"}),
        (
            "BitbucketConnectRequest",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": "00000000-0000-4000-8000-000000000334",
            },
        ),
        ("BitbucketCredentialRotateRequest", {}),
    ],
)
def test_bitbucket_token_request_contract_matches_printable_ascii_limit(
    schema_name: str, body: dict[str, str]
) -> None:
    validator = _validator_at(f"/components/schemas/{schema_name}")
    for token in ("a", "x" * 4096):
        validator.validate({**body, "apiToken": token})
    for token in ("", "has space", "bad\nline", "secret\n", "bad\u00a0char", "x" * 4097):
        with pytest.raises(ValidationError):
            validator.validate({**body, "apiToken": token})


@pytest.mark.parametrize(
    ("generated_name", "checked_name", "fields"),
    [
        ("DiscoveryRequest", "BitbucketDiscoveryRequest", ("apiToken", "bitbucketWorkspace")),
        (
            "ConnectRequest",
            "BitbucketConnectRequest",
            ("apiToken", "bitbucketWorkspace", "repositorySlug"),
        ),
        ("CredentialRotateRequest", "BitbucketCredentialRotateRequest", ("apiToken",)),
    ],
)
def test_bitbucket_request_constraints_match_generated_openapi(
    generated_name: str, checked_name: str, fields: tuple[str, ...]
) -> None:
    generated = app.openapi()["components"]["schemas"][generated_name]["properties"]
    checked = _spec()["components"]["schemas"][checked_name]["properties"]
    for field in fields:
        for constraint in ("type", "minLength", "maxLength", "pattern", "writeOnly"):
            assert generated[field].get(constraint) == checked[field].get(constraint), (
                generated_name,
                field,
                constraint,
            )


@pytest.mark.parametrize(
    "component",
    [
        "BitbucketDiscoveryRequest",
        "BitbucketConnectRequest",
        "BitbucketCredentialRotateRequest",
    ],
)
def test_bitbucket_api_token_schema_accepts_only_printable_ascii_up_to_4096(
    component: str,
) -> None:
    token_schema = _components()[component]["properties"]["apiToken"]
    validator = Draft202012Validator(token_schema)

    assert token_schema["writeOnly"] is True
    assert validator.is_valid("A" * 4096)
    for invalid in ("", "A" * 4097, "A token", "A\n", "A\x7f", "Aé"):
        assert not validator.is_valid(invalid)


@pytest.mark.parametrize(
    ("zod_name", "component"),
    [
        ("bitbucketDiscoveryRequest", "BitbucketDiscoveryRequest"),
        ("bitbucketConnectRequest", "BitbucketConnectRequest"),
        ("bitbucketCredentialRotateRequest", "BitbucketCredentialRotateRequest"),
    ],
)
def test_bitbucket_api_token_patterns_match_the_printable_ascii_contract(
    zod_name: str, component: str
) -> None:
    spec_token = _components()[component]["properties"]["apiToken"]
    ui_token = _generated_schemas()[zod_name]["properties"]["apiToken"]
    values = [
        "",
        *(chr(codepoint) for codepoint in range(128)),
        "é",
        "Ａ",
        "A\n",
        "A A",
        "A" * 4096,
        "A" * 4097,
    ]

    for source, schema in (("OpenAPI", spec_token), ("UI Zod", ui_token)):
        assert schema["type"] == "string", source
        assert (schema["minLength"], schema["maxLength"]) == (1, 4096), source
        pattern = schema["pattern"]
        assert pattern.startswith("^"), source
        assert pattern.endswith(("$", r"(?![\s\S])")), source
        for value in values:
            expected = 1 <= len(value) <= 4096 and all(
                33 <= ord(character) <= 126 for character in value
            )
            actual = 1 <= len(value) <= 4096 and re.fullmatch(pattern, value) is not None
            assert actual == expected, (source, component, repr(value[:20]))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/workspaces/{workspace_id}/bitbucket/discovery"),
        ("post", "/api/workspaces/{workspace_id}/bitbucket/connections"),
        (
            "put",
            "/api/workspaces/{workspace_id}/bitbucket/connections/{installation_id}/credential",
        ),
    ],
)
def test_bitbucket_error_contract_accepts_safe_application_and_request_validation(
    method: str, path: str
) -> None:
    operation = _spec()["paths"][path][method]
    _validator_for(method, path, "422").validate({"detail": "repository_inaccessible"})
    _validator_for(method, path, "422").validate(
        {"detail": [{"loc": ["body", "apiToken"], "msg": "Invalid value", "type": "value_error"}]}
    )
    rate_limited = operation["responses"]["429"]
    if method != "put":
        assert rate_limited == {"$ref": "#/components/responses/BitbucketRateLimited"}
        rate_limited = _spec()["components"]["responses"]["BitbucketRateLimited"]
    assert "Retry-After" in rate_limited["headers"]


_BITBUCKET_WORKSPACE = UUID(int=333)


# Zod 4 validates these response IDs as version-4 UUIDs.
_BITBUCKET_INSTALLATION = UUID("00000000-0000-4000-8000-00000000014e")


_BITBUCKET_REPOSITORY = UUID("00000000-0000-4000-8000-00000000014f")


_BITBUCKET_PREFIX = f"/api/workspaces/{_BITBUCKET_WORKSPACE}/bitbucket"


_BITBUCKET_CONNECTIONS = "/api/workspaces/{workspace_id}/bitbucket/connections"


_BITBUCKET_CREDENTIAL = f"{_BITBUCKET_CONNECTIONS}/{{installation_id}}/credential"


_BITBUCKET_REPOSITORY_PATH = (
    f"{_BITBUCKET_CONNECTIONS}/{{installation_id}}/repositories/{{repository_id}}"
)


class _BitbucketContractManager:
    def webhook_url(self, installation_id: UUID, repository_id: UUID) -> str:
        return f"https://example.test/webhooks/bitbucket/{installation_id}/{repository_id}"

    async def discover(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            repositories=(
                SimpleNamespace(
                    uuid=_BITBUCKET_REPOSITORY,
                    workspace_slug="team",
                    slug="repo",
                    name="Repo",
                    is_private=True,
                    html_url="https://bitbucket.org/team/repo",
                ),
            ),
            next_cursor=None,
        )

    async def list_connections_page(self, **_kwargs: object) -> ConnectionPage:
        return ConnectionPage(
            (
                RepositoryBinding(
                    installation_id=_BITBUCKET_INSTALLATION,
                    repository_id=_BITBUCKET_REPOSITORY,
                    repository_uuid=_BITBUCKET_REPOSITORY,
                    full_name="team/repo",
                    web_url="https://bitbucket.org/team/repo",
                    credential_generation=1,
                    binding_generation=1,
                    confirmed_at=datetime(2026, 10, 1, tzinfo=UTC),
                ),
            ),
            "next-page",
        )

    async def connect(self, **_kwargs: object) -> ConnectResult:
        return ConnectResult(
            installation_id=_BITBUCKET_INSTALLATION,
            repository_id=_BITBUCKET_REPOSITORY,
            repository_uuid=_BITBUCKET_REPOSITORY,
            workspace_slug="team",
            repository_slug="repo",
            credential_generation=1,
            webhook_secret="one-time-secret",
            confirmed=False,
        )

    async def rotate_token(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(installation_id=_BITBUCKET_INSTALLATION, generation=2)

    async def disconnect(self, **_kwargs: object) -> None:
        return None

    async def rotate_webhook_secret(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            url=self.webhook_url(_BITBUCKET_INSTALLATION, _BITBUCKET_REPOSITORY),
            secret="new-one-time-secret",
        )


@pytest.fixture
def bitbucket_contract_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (_BITBUCKET_WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(
        42, (_BITBUCKET_WORKSPACE,)
    )
    app.dependency_overrides[get_bitbucket_management] = _BitbucketContractManager
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("method", "path", "suffix", "body", "status"),
    [
        (
            "post",
            f"{_BITBUCKET_PREFIX}/discovery",
            "/discovery",
            {"bitbucketWorkspace": "team", "apiToken": "token"},
            "200",
        ),
        ("get", _BITBUCKET_CONNECTIONS, "/connections", None, "200"),
        (
            "post",
            _BITBUCKET_CONNECTIONS,
            "/connections",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(_BITBUCKET_REPOSITORY),
                "apiToken": "token",
            },
            "201",
        ),
        (
            "put",
            _BITBUCKET_CREDENTIAL,
            f"/connections/{_BITBUCKET_INSTALLATION}/credential",
            {"apiToken": "token"},
            "200",
        ),
        (
            "delete",
            _BITBUCKET_REPOSITORY_PATH,
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}",
            None,
            "204",
        ),
        (
            "post",
            f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret",
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret",
            None,
            "200",
        ),
    ],
)
def test_bitbucket_operation_response_matches_spec(
    bitbucket_contract_client: TestClient,
    method: str,
    path: str,
    suffix: str,
    body: dict[str, str] | None,
    status: str,
) -> None:
    spec_path = path.replace(str(_BITBUCKET_WORKSPACE), "{workspace_id}")
    response = bitbucket_contract_client.request(
        method.upper(), f"{_BITBUCKET_PREFIX}{suffix}", json=body
    )
    assert response.status_code == int(status)
    if status == "204":
        assert response.content == b""
        assert "content" not in _spec()["paths"][spec_path][method]["responses"][status]
        return
    payload = response.json()
    _validator_for(method, spec_path, status).validate(payload)
    if method == "post" and suffix == "/connections":
        with pytest.raises(ValidationError):
            _validator_for(method, spec_path, status).validate(
                {key: value for key, value in payload.items() if key != "webhook"}
            )
        with pytest.raises(ValidationError):
            _validator_for(method, spec_path, status).validate({**payload, "status": "bogus"})


def test_bitbucket_connection_page_contract_declares_cursor_query() -> None:
    operation = _spec()["paths"][_BITBUCKET_CONNECTIONS]["get"]
    cursor = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "cursor"
    )
    assert cursor["in"] == "query"
    assert cursor["required"] is False
    assert cursor["schema"] == {"type": "string", "minLength": 1, "maxLength": 1024}


@pytest.mark.parametrize(
    ("status", "reference"),
    [
        ("400", "BadRequest"),
        ("409", "BitbucketConflict"),
        ("422", "BitbucketValidationFailed"),
    ],
)
def test_bitbucket_connection_page_contract_declares_cursor_error_responses(
    status: str, reference: str
) -> None:
    operation = _spec()["paths"][_BITBUCKET_CONNECTIONS]["get"]
    assert operation["responses"][status] == {"$ref": f"#/components/responses/{reference}"}


def test_bitbucket_connection_cursor_runtime_schema_matches_checked_contract() -> None:
    runtime = app.openapi()["paths"][_BITBUCKET_CONNECTIONS]["get"]
    cursor = next(parameter for parameter in runtime["parameters"] if parameter["name"] == "cursor")
    checked = next(
        parameter
        for parameter in _spec()["paths"][_BITBUCKET_CONNECTIONS]["get"]["parameters"]
        if parameter["name"] == "cursor"
    )
    assert (cursor["in"], cursor["required"]) == (checked["in"], checked["required"])
    assert checked["schema"] in cursor["schema"]["anyOf"]


def test_bitbucket_connection_page_string_cursor_matches_openapi_and_ui(
    bitbucket_contract_client: TestClient,
) -> None:
    response = bitbucket_contract_client.get(f"{_BITBUCKET_PREFIX}/connections")
    assert response.status_code == 200
    payload = response.json()
    assert payload["nextCursor"] == "next-page"
    checked_validator = _validator_for("get", _BITBUCKET_CONNECTIONS, "200")
    ui_validator = Draft202012Validator(_generated_schemas()["bitbucketConnectionList"])
    for validator in (checked_validator, ui_validator):
        validator.validate(payload)
        validator.validate({**payload, "nextCursor": None})
        with pytest.raises(ValidationError):
            validator.validate({**payload, "nextCursor": 42})


def test_bitbucket_connect_and_secret_rotation_return_all_five_webhook_events(
    bitbucket_contract_client: TestClient,
) -> None:
    events = [
        "repo:push",
        "pullrequest:created",
        "pullrequest:updated",
        "pullrequest:fulfilled",
        "pullrequest:rejected",
    ]
    connected = bitbucket_contract_client.post(
        _BITBUCKET_CONNECTIONS.replace("{workspace_id}", str(_BITBUCKET_WORKSPACE)),
        json={
            "bitbucketWorkspace": "team",
            "repositorySlug": "repo",
            "repositoryUuid": str(_BITBUCKET_REPOSITORY),
            "apiToken": "token",
        },
    )
    rotated = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/connections/{_BITBUCKET_INSTALLATION}"
        f"/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret"
    )

    assert connected.status_code == 201
    assert rotated.status_code == 200
    assert connected.json()["webhook"]["events"] == events
    assert rotated.json()["events"] == events
    assert _components()["BitbucketWebhookSetup"]["properties"]["events"]["items"]["enum"] == events


@pytest.mark.parametrize(
    ("method", "path", "suffix", "body"),
    [
        (
            "post",
            "/api/workspaces/{workspace_id}/bitbucket/discovery",
            "/discovery",
            {"bitbucketWorkspace": "team", "apiToken": "token"},
        ),
        ("get", _BITBUCKET_CONNECTIONS, "/connections", None),
        (
            "post",
            _BITBUCKET_CONNECTIONS,
            "/connections",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(_BITBUCKET_REPOSITORY),
                "apiToken": "token",
            },
        ),
        (
            "put",
            _BITBUCKET_CREDENTIAL,
            f"/connections/{_BITBUCKET_INSTALLATION}/credential",
            {"apiToken": "token"},
        ),
        (
            "delete",
            _BITBUCKET_REPOSITORY_PATH,
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}",
            None,
        ),
        (
            "post",
            f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret",
            f"/connections/{_BITBUCKET_INSTALLATION}/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret",
            None,
        ),
    ],
)
def test_bitbucket_operation_not_found_body_matches_spec(
    bitbucket_contract_client: TestClient,
    method: str,
    path: str,
    suffix: str,
    body: dict[str, str] | None,
) -> None:
    class MissingManager:
        def __getattr__(self, _name: str) -> Any:
            async def missing(**_kwargs: object) -> None:
                raise BitbucketManagementError("workspace_not_found")

            return missing

    app.dependency_overrides[get_bitbucket_management] = MissingManager
    response = bitbucket_contract_client.request(
        method.upper(), f"{_BITBUCKET_PREFIX}{suffix}", json=body
    )
    assert response.status_code == 404
    _validator_for(method, path, "404").validate(response.json())


@pytest.mark.parametrize(
    "code,status",
    [
        ("invalid_credential", "400"),
        ("repository_inaccessible", "422"),
        ("provider_rate_limited", "429"),
    ],
)
def test_bitbucket_provider_error_body_matches_spec(
    bitbucket_contract_client: TestClient, code: str, status: str
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def discover(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketProviderError(code, retry_after=37 if status == "429" else None)

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    response = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/discovery", json={"bitbucketWorkspace": "team", "apiToken": "token"}
    )
    assert response.status_code == int(status)
    _validator_for("post", "/api/workspaces/{workspace_id}/bitbucket/discovery", status).validate(
        response.json()
    )
    if status == "429":
        assert response.headers["Retry-After"] == "37"


def test_rotation_error_responses_declare_repository_uuid_header() -> None:
    responses = _spec()["paths"][_BITBUCKET_CREDENTIAL]["put"]["responses"]
    for status in ("409", "422", "429", "502"):
        assert responses[status]["headers"]["X-Bitbucket-Repository-UUID"]["schema"] == {
            "type": "string",
            "format": "uuid",
        }
    assert "Retry-After" in responses["429"]["headers"]


def test_bitbucket_rotate_invalid_credential_response_matches_spec(
    bitbucket_contract_client: TestClient,
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def rotate_token(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketManagementError("invalid_credential")

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    response = bitbucket_contract_client.put(
        f"{_BITBUCKET_PREFIX}/connections/{_BITBUCKET_INSTALLATION}/credential",
        json={"apiToken": "token"},
    )
    assert response.status_code == 400
    _validator_for("put", _BITBUCKET_CREDENTIAL, "400").validate(response.json())


@pytest.mark.parametrize(
    ("method", "path", "suffix", "body"),
    [
        (
            "post",
            "/api/workspaces/{workspace_id}/bitbucket/discovery",
            "/discovery",
            {"bitbucketWorkspace": "team", "apiToken": "bad token"},
        ),
        (
            "post",
            _BITBUCKET_CONNECTIONS,
            "/connections",
            {
                "bitbucketWorkspace": "team",
                "repositorySlug": "repo",
                "repositoryUuid": str(_BITBUCKET_REPOSITORY),
                "apiToken": "bad token",
            },
        ),
        (
            "put",
            _BITBUCKET_CREDENTIAL,
            f"/connections/{_BITBUCKET_INSTALLATION}/credential",
            {"apiToken": "bad token"},
        ),
    ],
)
def test_bitbucket_request_validation_response_matches_spec_without_token_echo(
    bitbucket_contract_client: TestClient,
    method: str,
    path: str,
    suffix: str,
    body: dict[str, str],
) -> None:
    response = bitbucket_contract_client.request(
        method.upper(), f"{_BITBUCKET_PREFIX}{suffix}", json=body
    )
    assert response.status_code == 422
    assert body["apiToken"] not in response.text
    _validator_for(method, path, "422").validate(response.json())


@pytest.mark.parametrize(
    ("method", "path", "suffix"),
    [
        ("get", _BITBUCKET_CONNECTIONS, "/connections"),
        ("delete", _BITBUCKET_REPOSITORY_PATH, "/connections/not-a-uuid/repositories/not-a-uuid"),
        (
            "post",
            f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret",
            "/connections/not-a-uuid/repositories/not-a-uuid/webhook-secret",
        ),
    ],
)
def test_bitbucket_malformed_uuid_response_matches_spec(
    bitbucket_contract_client: TestClient, method: str, path: str, suffix: str
) -> None:
    if method == "get":
        url = "/api/workspaces/not-a-uuid/bitbucket/connections"
    else:
        url = f"{_BITBUCKET_PREFIX}{suffix}"
    response = bitbucket_contract_client.request(method.upper(), url)
    assert response.status_code == 422
    _validator_for(method, path, "422").validate(response.json())


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("bitbucket_connection_inactive", "409"),
        ("credential_unavailable", "503"),
    ],
)
def test_bitbucket_webhook_secret_error_body_matches_spec(
    bitbucket_contract_client: TestClient, code: str, status: str
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def rotate_webhook_secret(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketManagementError(code)

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    path = f"{_BITBUCKET_REPOSITORY_PATH}/webhook-secret"
    response = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/connections/{_BITBUCKET_INSTALLATION}"
        f"/repositories/{_BITBUCKET_REPOSITORY}/webhook-secret"
    )
    assert response.status_code == int(status)
    _validator_for("post", path, status).validate(response.json())


def test_bitbucket_provider_failure_body_matches_spec(
    bitbucket_contract_client: TestClient,
) -> None:
    class FailingManager(_BitbucketContractManager):
        async def discover(self, **_kwargs: object) -> SimpleNamespace:
            raise BitbucketProviderError("provider_unavailable")

    app.dependency_overrides[get_bitbucket_management] = FailingManager
    response = bitbucket_contract_client.post(
        f"{_BITBUCKET_PREFIX}/discovery", json={"bitbucketWorkspace": "team", "apiToken": "token"}
    )
    assert response.status_code == 502
    _validator_for("post", "/api/workspaces/{workspace_id}/bitbucket/discovery", "502").validate(
        response.json()
    )


@pytest.mark.parametrize("field", ["workspaceSlug", "repositorySlug"])
def test_bitbucket_response_slug_bounds_match_runtime_and_contract(
    bitbucket_contract_client: TestClient, field: str
) -> None:
    response = bitbucket_contract_client.post(
        _BITBUCKET_CONNECTIONS.replace("{workspace_id}", str(_BITBUCKET_WORKSPACE)),
        json={
            "bitbucketWorkspace": "team",
            "repositorySlug": "repo",
            "repositoryUuid": str(_BITBUCKET_REPOSITORY),
            "apiToken": "token",
        },
    )
    assert response.status_code == 201
    bad = {**response.json(), field: "x" * 129}
    with pytest.raises(PydanticValidationError):
        ConnectResponse.model_validate(bad)
    with pytest.raises(ValidationError):
        _validator_at("/components/schemas/BitbucketConnectResponse").validate(bad)

    listed = bitbucket_contract_client.get(
        _BITBUCKET_CONNECTIONS.replace("{workspace_id}", str(_BITBUCKET_WORKSPACE))
    )
    assert listed.status_code == 200
    bad_item = {**listed.json()["connections"][0], field: "x" * 129}
    with pytest.raises(PydanticValidationError):
        ConnectionItem.model_validate(bad_item)
    with pytest.raises(ValidationError):
        _validator_at("/components/schemas/BitbucketConnectionItem").validate(bad_item)
