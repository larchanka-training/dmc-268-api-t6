"""A rejected body that is not valid UTF-8 gets a JSON 422, never a 500 (#521).

The default client re-raises a server exception, so a passing request also proves the
handler raised nothing a server would log as a traceback.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NoReturn
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.bootstrap import auth_api
from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import get_repository_settings
from app.main import app
from app.modules.auth.application.scope import AuthScope
from tests.portal_test_client import authenticated_test_client

REPOSITORY_ID = UUID("00000000-0000-4000-8000-0000000000a2")
WORKSPACE = UUID("00000000-0000-4000-8000-000000000333")
BITBUCKET = f"/api/workspaces/{WORKSPACE}/bitbucket"

ROUTES_REPORTING_INPUT = [
    pytest.param("patch", f"/api/repos/{REPOSITORY_ID}", id="repository-update"),
    pytest.param("post", "/api/auth/github/callback", id="github-callback"),
]
BITBUCKET_ROUTES = [
    pytest.param("post", f"{BITBUCKET}/discovery", id="discovery"),
    pytest.param("post", f"{BITBUCKET}/connections", id="connect"),
    pytest.param("put", f"{BITBUCKET}/connections/{REPOSITORY_ID}/credential", id="credential"),
]
NON_JSON_CONTENT_TYPES = ["text/plain", "application/x-www-form-urlencoded", None]


def _unreachable() -> NoReturn:
    pytest.fail("a rejected body reached the use case")


def _send(
    client: TestClient, method: str, path: str, body: bytes, content_type: str | None
) -> httpx.Response:
    headers = {} if content_type is None else {"Content-Type": content_type}
    response: httpx.Response = client.request(method, path, content=body, headers=headers)
    return response


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_repository_settings] = lambda: _unreachable
    # Without a configured code exchange the callback answers 503 before validation.
    app.dependency_overrides[auth_api.get_exchange_github_code] = lambda: None
    try:
        yield authenticated_test_client(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "content_type", [*NON_JSON_CONTENT_TYPES, "multipart/form-data; boundary=x"]
)
@pytest.mark.parametrize(
    ("body", "reported_input"),
    [
        pytest.param(b"\xff", "\\xff", id="single-byte"),
        pytest.param(b"ok\xc3", "ok\\xc3", id="truncated-sequence"),
        pytest.param(b"\xed\xa0\x80", "\\xed\\xa0\\x80", id="encoded-surrogate"),
    ],
)
@pytest.mark.parametrize(("method", "path"), ROUTES_REPORTING_INPUT)
def test_invalid_utf8_without_json_content_type_is_a_json_422(
    client: TestClient,
    method: str,
    path: str,
    body: bytes,
    reported_input: str,
    content_type: str | None,
) -> None:
    response = _send(client, method, path, body, content_type)

    assert response.request.headers.get("content-type") == content_type
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    (error,) = response.json()["detail"]
    assert error["type"] == "model_attributes_type"
    assert error["loc"] == ["body"]
    assert error["input"] == reported_input


def test_invalid_utf8_with_an_invalid_path_reports_both_errors(client: TestClient) -> None:
    response = _send(client, "patch", "/api/repos/not-a-uuid", b"\xff", "text/plain")

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["path", "repo_id"],
        ["body"],
    ]


@pytest.mark.parametrize(("method", "path"), ROUTES_REPORTING_INPUT)
def test_invalid_utf8_with_json_content_type_stays_a_400(
    client: TestClient, method: str, path: str
) -> None:
    response = _send(client, method, path, b"\xff", "application/json")

    assert response.status_code == 400
    assert response.json() == {"detail": "There was an error parsing the body"}


@pytest.mark.parametrize("content_type", NON_JSON_CONTENT_TYPES)
@pytest.mark.parametrize(("method", "path"), ROUTES_REPORTING_INPUT)
def test_valid_text_without_json_content_type_keeps_the_fastapi_error(
    client: TestClient, method: str, path: str, content_type: str | None
) -> None:
    response = _send(client, method, path, b"hello", content_type)

    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["type"] == "model_attributes_type"
    assert error["loc"] == ["body"]
    assert error["input"] == "hello"


@pytest.mark.parametrize("content_type", NON_JSON_CONTENT_TYPES)
@pytest.mark.parametrize(("method", "path"), BITBUCKET_ROUTES)
def test_bitbucket_rejects_invalid_utf8_without_reporting_the_input(
    method: str, path: str, content_type: str | None
) -> None:
    # A workspace outside the claim answers 404 before validation.
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_bitbucket_management] = lambda: _unreachable
    try:
        response = _send(TestClient(app), method, path, b"\xff", content_type)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "model_attributes_type", "loc": ["body"], "msg": "Invalid input"}]
    }
