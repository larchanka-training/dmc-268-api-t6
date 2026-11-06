"""A request body over 64 KiB is refused while it is being read, never after it (#530).

``TestClient`` hands the body over in one message, which hides how much was read, so these
tests drive ``app`` as ASGI and count the calls to ``receive``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.types import Message, Scope

from app.bootstrap import auth_api
from app.bootstrap.bitbucket import (
    get_bitbucket_management,
    get_bitbucket_webhook_resources,
    get_bitbucket_workspace_scope,
)
from app.bootstrap.gitlab import get_gitlab_webhook_resources
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import (
    get_github_webhook_receipt_uow_factory,
    get_repository_settings,
)
from app.main import app, get_github_webhook_secret
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.gitlab.application.authenticate_webhook import (
    GitLabWebhookAuthStatus,
)
from tests.openapi_contract import _spec

REPOSITORY_ID = UUID("00000000-0000-4000-8000-0000000000a2")
WORKSPACE = UUID("00000000-0000-4000-8000-000000000333")
BITBUCKET = f"/api/workspaces/{WORKSPACE}/bitbucket"
ONE_MIB = 1024 * 1024
THIRTY_TWO_KIB = 32 * 1024

REPOSITORY_UPDATE = f"/api/repos/{REPOSITORY_ID}"
GITHUB_CALLBACK = "/api/auth/github/callback"
DISCOVERY = f"{BITBUCKET}/discovery"
CONNECT = f"{BITBUCKET}/connections"
CREDENTIAL = f"{BITBUCKET}/connections/{REPOSITORY_ID}/credential"

ROUTES_REPORTING_INPUT = [
    pytest.param("PATCH", REPOSITORY_UPDATE, id="repository-update"),
    pytest.param("POST", GITHUB_CALLBACK, id="github-callback"),
]
BITBUCKET_ROUTES = [
    pytest.param("POST", DISCOVERY, id="discovery"),
    pytest.param("POST", CONNECT, id="connect"),
    pytest.param("PUT", CREDENTIAL, id="credential"),
]


@dataclass(frozen=True)
class _Answer:
    status: int
    content_type: str
    body: bytes
    reads: int

    def json(self) -> Any:
        return json.loads(self.body)


def _call(
    method: str, path: str, chunks: list[bytes], headers: Mapping[str, str] | None = None
) -> _Answer:
    """Send ``chunks`` to ``app`` one ``http.request`` at a time and count what it reads."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    reads = 0
    status = 0
    content_type = ""
    body = bytearray()

    async def receive() -> Message:
        nonlocal reads
        if reads == len(chunks):
            return {"type": "http.disconnect"}
        reads += 1
        return {
            "type": "http.request",
            "body": chunks[reads - 1],
            "more_body": reads < len(chunks),
        }

    async def send(message: Message) -> None:
        nonlocal status, content_type
        if message["type"] == "http.response.start":
            status = message["status"]
            content_type = dict(message["headers"]).get(b"content-type", b"").decode()
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return _Answer(status, content_type, bytes(body), reads)


class _ReachedUseCase(Exception):
    pass


class _UseCase:
    """Stands in for every use case and unit-of-work factory behind the limited routes."""

    def __call__(self) -> NoReturn:
        raise _ReachedUseCase

    def __getattr__(self, name: str) -> NoReturn:
        raise _ReachedUseCase


def _install_token_and_use_cases() -> None:
    scope = AuthScope(42, (WORKSPACE,))
    app.dependency_overrides[get_auth_scope] = lambda: scope
    app.dependency_overrides[get_bitbucket_workspace_scope] = lambda: scope
    app.dependency_overrides[get_repository_settings] = _UseCase
    # Without a configured code exchange the callback answers 503 before validation.
    app.dependency_overrides[auth_api.get_exchange_github_code] = _UseCase
    app.dependency_overrides[get_bitbucket_management] = _UseCase


@pytest.fixture
def token() -> Iterator[None]:
    _install_token_and_use_cases()
    try:
        yield
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(params=[pytest.param(False, id="no-token"), pytest.param(True, id="token")])
def with_and_without_token(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.param:
        _install_token_and_use_cases()
    try:
        yield
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("chunks", "headers", "reads"),
    [
        pytest.param([b"x" * 65537], {"content-length": "65537"}, 0, id="declared-one-byte-over"),
        pytest.param([b"x" * 65537], {}, 1, id="chunked-one-byte-over"),
        pytest.param([b"x" * ONE_MIB] * 8, {}, 1, id="chunked-first-chunk-over"),
        pytest.param([b"x" * 65536, b"x", b"x"], {}, 2, id="chunked-crossing-on-the-second"),
        pytest.param([b"x" * ONE_MIB] * 8, {"content-length": "10"}, 1, id="understated-length"),
        pytest.param([b"x" * ONE_MIB] * 8, {"content-length": "abc"}, 1, id="malformed-length"),
    ],
)
@pytest.mark.parametrize(("method", "path"), [*ROUTES_REPORTING_INPUT, *BITBUCKET_ROUTES])
@pytest.mark.usefixtures("with_and_without_token")
def test_oversized_body_gets_a_json_413_without_being_read_further(
    method: str, path: str, chunks: list[bytes], headers: dict[str, str], reads: int
) -> None:
    answer = _call(method, path, chunks, headers)

    assert answer.status == 413
    assert answer.content_type == "application/json"
    assert answer.json() == {"detail": "payload too large"}
    assert answer.reads == reads


@pytest.mark.parametrize(
    ("body", "content_type", "error_type", "reported_input"),
    [
        pytest.param(b"x" * 65536, "text/plain", "model_attributes_type", "x" * 65536, id="text"),
        pytest.param(
            b"\xff" * 65536,
            "text/plain",
            "model_attributes_type",
            "\\xff" * 65536,
            id="invalid-utf8-521",
        ),
        pytest.param(
            b'{"x": NaN}'.ljust(65536), "application/json", "json_invalid", {}, id="nonfinite-235"
        ),
    ],
)
@pytest.mark.parametrize(("method", "path"), ROUTES_REPORTING_INPUT)
@pytest.mark.usefixtures("token")
def test_body_at_the_limit_keeps_its_validation_error(
    method: str,
    path: str,
    body: bytes,
    content_type: str,
    error_type: str,
    reported_input: object,
) -> None:
    answer = _call(
        method, path, [body], {"content-type": content_type, "content-length": str(len(body))}
    )

    assert (answer.status, answer.reads) == (422, 1)
    (error,) = answer.json()["detail"]
    assert error["type"] == error_type
    assert error["input"] == reported_input


@pytest.mark.parametrize(("method", "path"), ROUTES_REPORTING_INPUT)
@pytest.mark.usefixtures("token")
def test_invalid_utf8_at_the_limit_with_json_content_type_stays_a_400(
    method: str, path: str
) -> None:
    answer = _call(method, path, [b"\xff" * 65536], {"content-type": "application/json"})

    assert answer.status == 400
    assert answer.json() == {"detail": "There was an error parsing the body"}


@pytest.mark.parametrize(("method", "path"), BITBUCKET_ROUTES)
@pytest.mark.usefixtures("token")
def test_bitbucket_body_at_the_limit_keeps_its_redacted_error(method: str, path: str) -> None:
    answer = _call(method, path, [b"x" * 65536], {"content-type": "text/plain"})

    assert answer.status == 422
    assert answer.json() == {
        "detail": [{"type": "model_attributes_type", "loc": ["body"], "msg": "Invalid input"}]
    }


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        pytest.param("PATCH", REPOSITORY_UPDATE, b'{"enabled": true}', id="repository-update"),
        pytest.param("POST", GITHUB_CALLBACK, b'{"code": "abc"}', id="github-callback"),
        pytest.param(
            "POST",
            DISCOVERY,
            b'{"apiToken": "token", "bitbucketWorkspace": "acme"}',
            id="discovery",
        ),
        pytest.param(
            "POST",
            CONNECT,
            b'{"apiToken": "token", "bitbucketWorkspace": "acme", "repositorySlug": "repo",'
            b' "repositoryUuid": "00000000-0000-4000-8000-0000000000a2"}',
            id="connect",
        ),
        pytest.param("PUT", CREDENTIAL, b'{"apiToken": "token"}', id="credential"),
    ],
)
@pytest.mark.usefixtures("token")
def test_valid_json_of_exactly_64_kib_reaches_the_use_case(
    method: str, path: str, payload: bytes
) -> None:
    body = payload.ljust(65536)
    assert len(body) == 65536

    with pytest.raises(_ReachedUseCase):
        _call(
            method,
            path,
            [body[:THIRTY_TWO_KIB], body[THIRTY_TWO_KIB:]],
            {"content-type": "application/json", "content-length": "65536"},
        )


class _GitLabResources:
    async def authenticate(self, *, connection_id: UUID, secret: str) -> GitLabWebhookAuthStatus:
        return GitLabWebhookAuthStatus.AUTHENTICATED


class _BitbucketResources:
    async def authenticate(
        self, *, installation_id: UUID, repository_id: UUID, body: bytes, signature: str | None
    ) -> None:
        return None


@pytest.fixture
def webhooks() -> Iterator[None]:
    app.dependency_overrides[get_github_webhook_secret] = lambda: "secret"
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = _UseCase
    app.dependency_overrides[get_gitlab_webhook_resources] = _GitLabResources
    app.dependency_overrides[get_bitbucket_webhook_resources] = _BitbucketResources
    try:
        yield
    finally:
        app.dependency_overrides.clear()


GITHUB_WEBHOOK = "/webhooks/github"
GITLAB_WEBHOOK = f"/webhooks/gitlab/{REPOSITORY_ID}"
GITLAB_HEADERS = {"x-gitlab-token": "secret", "x-gitlab-event": "Push Hook"}
BITBUCKET_WEBHOOK = f"/webhooks/bitbucket/{REPOSITORY_ID}/{WORKSPACE}"
FIVE_CHUNKS = [b"x" * THIRTY_TWO_KIB] * 5
FIVE_CHUNKS_LENGTH = {"content-length": str(5 * THIRTY_TWO_KIB)}


@pytest.mark.parametrize(
    ("path", "chunks", "headers", "status", "reads"),
    [
        # A rejection after the read proves the whole body got through, unlike a bare "not 413".
        pytest.param(GITHUB_WEBHOOK, FIVE_CHUNKS, FIVE_CHUNKS_LENGTH, 401, 5, id="github-within"),
        pytest.param(GITHUB_WEBHOOK, [b"x" * ONE_MIB] * 11, {}, 413, 11, id="github-over-10-mib"),
        pytest.param(
            GITLAB_WEBHOOK,
            FIVE_CHUNKS,
            {**GITLAB_HEADERS, **FIVE_CHUNKS_LENGTH},
            400,
            5,
            id="gitlab-within",
        ),
        pytest.param(
            GITLAB_WEBHOOK, [b"x" * ONE_MIB] * 11, GITLAB_HEADERS, 413, 11, id="gitlab-over-10-mib"
        ),
        pytest.param(
            BITBUCKET_WEBHOOK, FIVE_CHUNKS, FIVE_CHUNKS_LENGTH, 401, 5, id="bitbucket-within"
        ),
        pytest.param(
            BITBUCKET_WEBHOOK,
            [b"x" * THIRTY_TWO_KIB] * 9,
            {},
            413,
            9,
            id="bitbucket-over-256-kib",
        ),
    ],
)
@pytest.mark.usefixtures("webhooks")
def test_webhooks_keep_their_own_limits(
    path: str, chunks: list[bytes], headers: dict[str, str], status: int, reads: int
) -> None:
    answer = _call("POST", path, chunks, headers)

    assert (answer.status, answer.reads) == (status, reads)


def _routes_with_a_parsed_body() -> set[tuple[str, str]]:
    return {
        (method, route.path_format)
        for route in iter_route_contexts(app.routes)
        if isinstance(route.original_route, APIRoute) and route.body_field is not None
        for method in route.methods or ()
    }


def test_every_route_with_a_parsed_body_answers_and_declares_413() -> None:
    routes = _routes_with_a_parsed_body()
    assert routes >= {
        ("PATCH", "/api/repos/{repo_id}"),
        ("POST", "/api/auth/github/callback"),
        ("POST", "/api/workspaces/{workspace_id}/bitbucket/discovery"),
        ("POST", "/api/workspaces/{workspace_id}/bitbucket/connections"),
        (
            "PUT",
            "/api/workspaces/{workspace_id}/bitbucket/connections/{installation_id}/credential",
        ),
    }

    for method, path in sorted(routes):
        url = re.sub(r"\{[^}]+\}", str(REPOSITORY_ID), path)
        answer = _call(method, url, [b"x" * 65537, b"x"])
        assert (answer.status, answer.reads) == (413, 1), (method, path)
        assert "413" in app.openapi()["paths"][path][method.lower()]["responses"], (method, path)
        assert "413" in _spec()["paths"][path][method.lower()]["responses"], (method, path)
