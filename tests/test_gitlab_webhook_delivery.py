"""Public GitLab HTTP receipt boundary and isolated PostgreSQL durability."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import aio_pika
import httpx
import pytest
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.types import Message, Scope

from alembic import command
from app.bootstrap.gitlab import (
    GitLabWebhookResources,
    build_gitlab_webhook_resources,
    get_gitlab_webhook_resources,
    gitlab_webhook_lifespan,
)
from app.main import app
from app.modules.integrations.gitlab.application.authenticate_webhook import GitLabWebhookAuthStatus
from app.modules.integrations.gitlab.infrastructure.credential_cipher import digest_webhook_secret
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt

CONNECTION = UUID("00000000-0000-0000-0000-000000000071")
SECRET = "synthetic-webhook-secret-canary-0001"
BODY = (
    b'{"object_kind":"merge_request","project":{"id":17},"obj'
    b'ect_attributes":{"id":91,"iid":7,"action":"open"}}'
)
HEADERS = {
    "X-Gitlab-Token": SECRET,
    "X-Gitlab-Event": "Merge Request Hook",
    "webhook-id": "stable-1",
}


class Authenticator:
    def __init__(
        self, status: GitLabWebhookAuthStatus = GitLabWebhookAuthStatus.AUTHENTICATED
    ) -> None:
        self.status = status
        self.calls = 0

    async def execute(self, *, connection_id: UUID, secret: str | None) -> GitLabWebhookAuthStatus:
        self.calls += 1
        return self.status


class Acceptor:
    def __init__(self) -> None:
        self.receipts: list[WebhookReceipt] = []
        self.error: Exception | None = None
        self.status = InstallationDeliveryDispatchStatus.PENDING

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
        if self.error is not None:
            raise self.error
        self.receipts.append(delivery)
        return self.status


def test_mounted_route_accepts_original_payload_after_acceptance() -> None:
    async def scenario() -> None:
        acceptor = Acceptor()
        app.state.gitlab_webhook_resources = GitLabWebhookResources(
            authenticator=Authenticator(), receiver=acceptor, intake_enabled=True
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/webhooks/gitlab/{CONNECTION}", content=BODY, headers=HEADERS
                )
            assert response.status_code == 202
            assert response.json() == {"status": "pending"}
            assert len(acceptor.receipts) == 1
            assert acceptor.receipts[0].payload_json == BODY.decode()
            assert acceptor.receipts[0].origin_provider_installation_id == CONNECTION
        finally:
            del app.state.gitlab_webhook_resources

    asyncio.run(scenario())


@contextmanager
def mounted(resources: GitLabWebhookResources | None) -> Iterator[None]:
    if resources is not None:
        app.state.gitlab_webhook_resources = resources
    try:
        yield
    finally:
        if resources is not None:
            del app.state.gitlab_webhook_resources


def resources_for(
    auth: Authenticator, acceptor: Acceptor, *, enabled: bool = True
) -> GitLabWebhookResources:
    return GitLabWebhookResources(authenticator=auth, receiver=acceptor, intake_enabled=enabled)


async def post(
    body: bytes = BODY,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    origin: str = str(CONNECTION),
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/webhooks/gitlab/{origin}",
            content=body,
            headers=httpx.Headers(HEADERS if headers is None else headers, encoding="latin-1"),
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [(GitLabWebhookAuthStatus.INVALID_CREDENTIALS, 401), (GitLabWebhookAuthStatus.DISABLED, 503)],
)
def test_authentication_precedes_body(status: GitLabWebhookAuthStatus, expected: int) -> None:
    auth, acceptor = Authenticator(status), Acceptor()
    with mounted(resources_for(auth, acceptor)):
        response = asyncio.run(post(b"invalid-body-canary"))
    assert response.status_code == expected
    assert not acceptor.receipts
    assert "canary" not in response.text


@pytest.mark.parametrize("origin", ["malformed-origin-canary", "0", ""])
def test_invalid_origin_is_uniform_unauthorized(origin: str) -> None:
    # Empty path has no route; use a nonempty invalid selection in that case.
    auth, acceptor = Authenticator(), Acceptor()
    with mounted(resources_for(auth, acceptor)):
        response = asyncio.run(post(origin=origin or "null"))
    assert response.status_code == 401
    assert response.json() == {"detail": "gitlab_webhook_unauthorized"}
    assert not acceptor.receipts
    assert auth.calls == 0


@pytest.mark.parametrize("enabled", [None, False])
def test_unconfigured_or_globally_disabled_precedes_auth(enabled: bool | None) -> None:
    auth, acceptor = Authenticator(), Acceptor()
    resources = None if enabled is None else resources_for(auth, acceptor, enabled=enabled)
    with mounted(resources):
        response = asyncio.run(post(b"invalid-body-canary", origin="invalid"))
    assert response.status_code == 503
    assert auth.calls == 0
    assert not acceptor.receipts


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("X-Gitlab-Token", None, 401),
        ("X-Gitlab-Token", "second-token-canary", 401),
        ("X-Gitlab-Event", None, 400),
        ("X-Gitlab-Event", "Merge Request Hook", 400),
        ("webhook-id", "stable-1", 400),
        ("Idempotency-Key", "stable-1", 400),
    ],
)
def test_required_headers_reject_missing_or_repeated(
    name: str, value: str | None, expected: int
) -> None:
    headers = [(k, v) for k, v in HEADERS.items() if value is not None or k != name]
    if value is not None:
        if name == "Idempotency-Key":
            headers.append((name, value))
        headers.append((name.lower(), value))
    auth, acceptor = Authenticator(), Acceptor()
    with mounted(resources_for(auth, acceptor)):
        response = asyncio.run(post(headers=headers))
    assert response.status_code == expected
    assert not acceptor.receipts
    assert "canary" not in response.text


def test_accept_failure_never_acknowledges_or_renders_canaries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth, acceptor = Authenticator(), Acceptor()
    canaries = (
        SECRET,
        digest_webhook_secret(SECRET),
        "postgresql://canary:DSN-canary@test/db",
        "input-body-canary",
    )
    acceptor.error = RuntimeError(" ".join(canaries))
    with mounted(resources_for(auth, acceptor)):
        response = asyncio.run(post())
    assert response.status_code == 503
    assert response.json() == {"detail": "gitlab_webhook_unavailable"}
    for canary in canaries:
        assert canary not in response.text + caplog.text
    assert not acceptor.receipts


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b"{",
        b'{"a":1,"a":2}',
        b'{"nested":{"a":1,"a":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":"\\u0000"}',
        b'{"\\u0000":1}',
        b'{"x":"\\ud800"}',
        b'{"\\udfff":1}',
        b'{"x":"\xff"}',
        b"\xef\xbb\xbf{}",
        "{}".encode("utf-16"),
        b'{"x":' + b"1" * 5000 + b"}",
        b'{"x":' + b"[" * 129 + b"0" + b"]" * 129 + b"}",
        b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    ],
)
def test_strict_json_rejected_before_acceptance(body: bytes) -> None:
    acceptor = Acceptor()
    headers = {**HEADERS, "X-Gitlab-Event": "System Hook"}
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(body, headers))
    assert response.status_code == 400
    assert not acceptor.receipts


@pytest.mark.parametrize(
    ("where", "name", "value"),
    [
        ("root", "object_kind", "push"),
        ("root", "event_type", "push"),
        ("root", "event_type", None),
        ("root", "object_attributes", []),
        ("root", "project", None),
        ("attributes", "id", None),
        ("attributes", "id", 0),
        ("attributes", "id", True),
        ("attributes", "id", "91"),
        ("attributes", "id", 91.0),
        ("attributes", "id", 9223372036854775808),
        ("attributes", "iid", 2147483648),
        ("attributes", "iid", -1),
        ("attributes", "iid", False),
        ("attributes", "iid", "7"),
        ("attributes", "iid", 7.0),
        ("attributes", "iid", None),
        ("attributes", "action", ""),
        ("attributes", "action", "a" * 101),
        ("attributes", "action", True),
        ("attributes", "action", None),
        ("attributes", "target_project_id", 99),
        ("attributes", "target_project_id", 17.0),
        ("attributes", "target_project_id", None),
        ("project", "id", 0),
        ("project", "id", True),
        ("project", "id", "17"),
        ("project", "id", 17.0),
        ("project", "id", 9223372036854775808),
    ],
)
def test_mr_identity_is_strict(where: str, name: str, value: object) -> None:
    payload = json.loads(BODY)
    target = (
        payload
        if where == "root"
        else payload["project" if where == "project" else "object_attributes"]
    )
    target[name] = value
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(json.dumps(payload).encode()))
    assert response.status_code == 400
    assert not acceptor.receipts


@pytest.mark.parametrize("length", ["-1", "+1", " 1", "1 ", "1.0", "x", "9" * 5000, ""])
def test_invalid_content_length(length: str) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(headers={**HEADERS, "Content-Length": length}))
    assert response.status_code == 400
    assert not acceptor.receipts


def test_repeated_content_length() -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(
            post(
                headers=[
                    *HEADERS.items(),
                    ("Content-Length", str(len(BODY))),
                    ("content-length", str(len(BODY))),
                ]
            )
        )
    assert response.status_code == 400
    assert not acceptor.receipts


def test_advertised_oversize() -> None:
    with mounted(resources_for(Authenticator(), Acceptor())):
        response = asyncio.run(post(headers={**HEADERS, "Content-Length": "10485761"}))
    assert response.status_code == 413


async def raw_request(
    chunks: list[bytes], extra_headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[int, int]:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/webhooks/gitlab/{CONNECTION}",
        "raw_path": f"/webhooks/gitlab/{CONNECTION}".encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()]
        + (extra_headers or []),
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    reads = 0
    status = 0

    async def receive() -> Message:
        nonlocal reads
        assert reads < len(chunks), "stream unread-next-chunk sentinel"
        chunk = chunks[reads]
        reads += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    await app(scope, receive, send)
    return status, reads


@pytest.mark.parametrize("advertised", [None, b"1", b"10485760"])
def test_stream_cap_stops_without_reading_next_chunk(advertised: bytes | None) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        status, reads = asyncio.run(
            raw_request(
                [b"x" * 10485760, b"x"],
                [] if advertised is None else [(b"content-length", advertised)],
            )
        )
    assert (status, reads) == (413, 2)
    assert not acceptor.receipts


OTHER = UUID("00000000-0000-0000-0000-000000000072")
GITHUB = UUID("00000000-0000-0000-0000-000000000073")
UNKNOWN = UUID("00000000-0000-0000-0000-000000000074")
OTHER_SECRET = "synthetic-webhook-secret-canary-0002"


@dataclass(frozen=True)
class Database:
    url: str = field(repr=False)
    schema: str

    @asynccontextmanager
    async def sessions(self) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
        engine = create_async_engine(
            self.url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_webhook_delivery_{uuid4().hex}"
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for number, origin, secret, provider in (
                (71, CONNECTION, SECRET, "gitlab"),
                (72, OTHER, OTHER_SECRET, "gitlab"),
                (71, GITHUB, SECRET, "github"),
            ):
                workspace = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'T7b', 7)"
                    ),
                    {"id": workspace},
                )
                connection.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id, workspace_id, provider, external_id, metadata) "
                        "VALUES (:id, :workspace, :provider, :number, '{}')"
                    ),
                    {"id": origin, "workspace": workspace, "number": number, "provider": provider},
                )
                if provider == "gitlab":
                    instance = f"https://gitlab{number}.example"
                    connection.execute(
                        text(
                            "INSERT INTO gitlab_connections "
                            "(provider_installation_id, workspace_id, instance_origin, "
                            "token_ciphertext, credential_key_id, webhook_secret_digest, "
                            "intake_enabled) VALUES (:id, :workspace, :instance, "
                            ":envelope, 'missing-pat-key', :digest, true)"
                        ),
                        {
                            "id": origin,
                            "workspace": workspace,
                            "instance": instance,
                            "envelope": b"synthetic-pat-envelope-canary",
                            "digest": digest_webhook_secret(secret),
                        },
                    )
                    repository = uuid4()
                    connection.execute(
                        text(
                            "INSERT INTO repositories (id, provider_installation_id, "
                            "external_id, full_name, default_branch, web_url) VALUES "
                            "(:id, :origin, 17, 'team/repo', 'main', :url)"
                        ),
                        {"id": repository, "origin": origin, "url": instance + "/team/repo"},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO gitlab_project_bindings (repository_id, "
                            "provider_installation_id, instance_origin, project_external_id) "
                            "VALUES (:id, :origin, :instance, 17)"
                        ),
                        {"id": repository, "origin": origin, "instance": instance},
                    )
            connection.commit()
        yield Database(url, schema)
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(("literal", "expected"), [("1e-100000", 400), ("1e-16383", 202)])
def test_jsonb_numeric_fractional_boundary(database: Database, literal: str, expected: int) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            body = BODY[:-1] + b',"tiny":' + literal.encode() + b"}"
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                response = await post(body)
            assert response.status_code == expected
            async with sessions() as session:
                count = (
                    await session.execute(text("SELECT count(*) FROM webhook_events"))
                ).scalar_one()
                assert count == (1 if expected == 202 else 0)
                if expected == 202:
                    exact = (
                        await session.execute(text("SELECT payload->>'tiny' FROM webhook_events"))
                    ).scalar_one()
                    assert exact == "0." + "0" * 16382 + "1"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": "stable-1"},
        {"webhook-id": "stable-1", "Idempotency-Key": "stable-1"},
        {"WeBhOoK-Id": "stable-1"},
        {"webhook-id": "!" * 255},
    ],
)
def test_stable_header_fallback_agreement_and_case(headers: dict[str, str]) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(
            post(headers={k: v for k, v in HEADERS.items() if k != "webhook-id"} | headers)
        )
    assert response.status_code == 202
    assert len(acceptor.receipts) == 1
    assert acceptor.receipts[0].delivery_id.startswith(f"gitlab:{CONNECTION}:")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Gitlab-Webhook-UUID": "not-a-stable-key"},
        {"X-Gitlab-Event-UUID": "not-a-stable-key"},
        {"webhook-id": "a", "Idempotency-Key": "b"},
        {"webhook-id": ""},
        {"webhook-id": "two words"},
        {"webhook-id": " key"},
        {"webhook-id": "key "},
        {"webhook-id": "x" * 256},
        {"webhook-id": "é"},
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "x" * 256},
        {"webhook-id": "stable-1", "Idempotency-Key": "bad key"},
        {"webhook-id": "gitlab:already-normalized"},
    ],
)
def test_invalid_stable_identity(headers: dict[str, str]) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(
            post(headers={k: v for k, v in HEADERS.items() if k != "webhook-id"} | headers)
        )
    assert response.status_code == 400
    assert not acceptor.receipts


@pytest.mark.parametrize("event", ["", "1Hook", "x" * 101, "Hook!", " Hook", "Hook\t"])
def test_event_header_bounds(event: str) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(headers={**HEADERS, "X-Gitlab-Event": event}))
    assert response.status_code == 400
    assert not acceptor.receipts


@pytest.mark.parametrize("action", ["open", "reopen", "update", "close", "merge", "future_action"])
def test_known_and_valid_unknown_actions(action: str) -> None:
    body = BODY.replace(b'"open"', json.dumps(action).encode())
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(body))
    assert response.status_code == 202
    assert acceptor.receipts[0].payload_json == body.decode()


def test_maximum_mr_identity_and_fork_source_are_accepted() -> None:
    body = (
        b'{"object_kind":"merge_request","event_type":"merge_requ'
        b'est","project":{"id":9223372036854775807},"object_attri'
        b'butes":{"id":9223372036854775807,"iid":2147483647,"acti'
        b'on":"open","target_project_id":9223372036854775807,"sou'
        b'rce_project_id":99}}'
    )
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(body))
    assert response.status_code == 202
    assert acceptor.receipts[0].payload_json == body.decode()


@pytest.mark.parametrize(
    "body",
    [
        b'{"unicode":"\\ud83d\\ude00"}',
        '{"unicode":"é😀"}'.encode(),
        b'{"x":' + b"[" * 127 + b"0" + b"]" * 127 + b"}",
        b'{"tiny":0e131073}',
        b'{"tiny":100.00e-16381}',
        b'{"tiny":1e-00000000000000000000016383}',
    ],
)
def test_valid_strict_json_controls(body: bytes) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(body, {**HEADERS, "X-Gitlab-Event": "System Hook"}))
    assert response.status_code == 202
    assert acceptor.receipts[0].payload_json == body.decode()


@pytest.mark.parametrize(
    "literal",
    [
        "1e-16384",
        "1.0e-16383",
        "10e-16384",
        "0e-100000",
        "1e-99999999999999999999999999999999999",
        "1e131072",
        "0e1073741824",
    ],
)
def test_jsonb_numeric_lexeme_bounds(literal: str) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(
            post(b'{"x":' + literal.encode() + b"}", {**HEADERS, "X-Gitlab-Event": "System Hook"})
        )
    assert response.status_code == 400
    assert not acceptor.receipts


def test_exact_body_limit_is_accepted_without_reserialization() -> None:
    body = BODY + b" " * (10485760 - len(BODY))
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(body))
    assert response.status_code == 202
    assert acceptor.receipts[0].payload_json == body.decode()


def test_declared_actual_length_mismatch() -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(headers={**HEADERS, "Content-Length": "1"}))
    assert response.status_code == 400
    assert not acceptor.receipts


def test_auth_and_disabled_requests_never_consume_a_chunk() -> None:
    for resources, expected in [
        (None, 503),
        (
            resources_for(Authenticator(GitLabWebhookAuthStatus.INVALID_CREDENTIALS), Acceptor()),
            401,
        ),
        (resources_for(Authenticator(GitLabWebhookAuthStatus.DISABLED), Acceptor()), 503),
    ]:
        with mounted(resources):
            assert asyncio.run(raw_request([])) == (expected, 0)


def test_acknowledgement_waits_for_acceptance_completion() -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class WaitingAcceptor:
            async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
                entered.set()
                await release.wait()
                return InstallationDeliveryDispatchStatus.DUPLICATE

        resources = GitLabWebhookResources(
            authenticator=Authenticator(), receiver=WaitingAcceptor(), intake_enabled=True
        )
        with mounted(resources):
            task = asyncio.create_task(post())
            await asyncio.wait_for(entered.wait(), 1)
            assert not task.done()
            release.set()
            response = await task
        assert response.status_code == 202
        assert response.json() == {"status": "duplicate"}

    asyncio.run(scenario())


@pytest.mark.integration
def test_pg_ack_is_committed_precise_and_origin_pinned(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbid_http(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider/GitHub HTTP forbidden during acknowledgement")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbid_http)

    def forbid_queue(*args: object, **kwargs: object) -> None:
        raise AssertionError("queue connection forbidden during acknowledgement")

    monkeypatch.setattr(aio_pika, "connect_robust", forbid_queue)
    monkeypatch.setattr(aio_pika, "RobustConnection", forbid_queue)

    async def scenario() -> None:
        async with database.sessions() as sessions:
            precise = b"0.123456789012345678901234567890123456789"
            body = (
                BODY[:-1]
                + b',"decimal":'
                + precise
                + b',"installation":{"id":999},"workspace_id":"payload-owner-canary"}'
            )
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                response = await post(body)
                assert response.status_code == 202
                assert response.json() == {"status": "pending"}
                # A new session after the HTTP response must already observe COMMIT.
                async with sessions() as session:
                    before = (
                        (
                            await session.execute(
                                text(
                                    (
                                        "SELECT id,provider,provider_installation_id,gitlab_conn"
                                        "ection_id,installation_external_id,event,action,payload"
                                        "->>'decimal' AS exact,payload->'project'->>'id' AS proj"
                                        "ect,projected_at,delivery_id FROM webhook_events"
                                    )
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert before["provider"] == "gitlab"
                    assert before["provider_installation_id"] == CONNECTION
                    assert before["gitlab_connection_id"] == CONNECTION
                    assert before["installation_external_id"] == 71
                    assert before["event"] == "Merge Request Hook"
                    assert before["action"] == "open"
                    assert before["exact"] == "0.123456789012345678901234567890123456789"
                    assert before["project"] == "17"
                    assert before["projected_at"] is None
                duplicate = await post(body)
                assert duplicate.status_code == 202
                assert duplicate.json() == {"status": "duplicate"}
                assert (
                    await post(body.replace(precise, b"0.123456789012345678901234567890123456788"))
                ).status_code == 400
                assert (
                    await post(body, {**HEADERS, "X-Gitlab-Event": "Push Hook"})
                ).status_code == 400
            async with sessions() as session:
                after = (
                    (
                        await session.execute(
                            text(
                                (
                                    "SELECT id,provider,provider_installation_id,gitlab_conn"
                                    "ection_id,installation_external_id,event,action,payload"
                                    "->>'decimal' AS exact,payload->'project'->>'id' AS proj"
                                    "ect,projected_at,delivery_id FROM webhook_events"
                                )
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert dict(before) == dict(after)
                assert (await session.execute(text("SELECT count(*) FROM runs"))).scalar_one() == 0
                assert (
                    await session.execute(text("SELECT count(*) FROM code_changes"))
                ).scalar_one() == 0

    asyncio.run(scenario())


@pytest.mark.integration
def test_pg_concurrent_duplicate_and_origin_isolation(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                responses = await asyncio.gather(post(), post())
                assert [r.status_code for r in responses] == [202, 202]
                assert sorted(r.json()["status"] for r in responses) == ["duplicate", "pending"]
                response = await post(
                    headers={**HEADERS, "X-Gitlab-Token": OTHER_SECRET}, origin=str(OTHER)
                )
                assert response.status_code == 202
            async with sessions() as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                (
                                    "SELECT provider_installation_id,gitlab_connection_id,in"
                                    "stallation_external_id,delivery_id,payload->'project'->"
                                    ">'id' AS project FROM webhook_events ORDER BY installat"
                                    "ion_external_id"
                                )
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(rows) == 2
                assert [
                    (
                        r["provider_installation_id"],
                        r["gitlab_connection_id"],
                        r["installation_external_id"],
                        r["project"],
                    )
                    for r in rows
                ] == [(CONNECTION, CONNECTION, 71, "17"), (OTHER, OTHER, 72, "17")]
                assert rows[0]["delivery_id"] != rows[1]["delivery_id"]

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("origin", "token"),
    [
        (str(UNKNOWN), SECRET),
        (str(GITHUB), SECRET),
        (str(CONNECTION), OTHER_SECRET),
        (str(OTHER), SECRET),
        (str(CONNECTION), None),
        (str(CONNECTION), "short"),
        (str(CONNECTION), "é" * 32),
        (str(CONNECTION), "x" * 257),
        (str(CONNECTION), " " * 32),
    ],
)
def test_pg_uniform_authentication_failures(
    database: Database, origin: str, token: str | None
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            headers = {k: v for k, v in HEADERS.items() if k != "X-Gitlab-Token"}
            if token is not None:
                headers["X-Gitlab-Token"] = token
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                response = await post(b"invalid-body-canary", headers, origin)
            assert response.status_code == 401
            assert response.json() == {"detail": "gitlab_webhook_unauthorized"}
            async with sessions() as session:
                assert (
                    await session.execute(text("SELECT count(*) FROM webhook_events"))
                ).scalar_one() == 0

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            (
                "UPDATE gitlab_connections SET revoked_at=now(),intake_e"
                "nabled=false WHERE provider_installation_id=:id"
            ),
            401,
        ),
        ("UPDATE provider_installations SET deleted_at=now() WHERE id=:id", 401),
        (
            "UPDATE gitlab_connections SET intake_enabled=false WHERE provider_installation_id=:id",
            503,
        ),
        ("DELETE FROM gitlab_project_bindings WHERE provider_installation_id=:id", 400),
        ("UPDATE repositories SET enabled=false WHERE provider_installation_id=:id", 400),
        (
            (
                "UPDATE repositories SET installation_available=false WH"
                "ERE provider_installation_id=:id"
            ),
            400,
        ),
    ],
)
def test_pg_live_admission_state(database: Database, change: str, expected: int) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            async with sessions() as session:
                await session.execute(text(change), {"id": CONNECTION})
                await session.commit()
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                response = await post()
                assert response.status_code == expected
                # Wrong credentials cannot disclose disabled state.
                assert (
                    await post(headers={**HEADERS, "X-Gitlab-Token": OTHER_SECRET})
                ).status_code == 401
            async with sessions() as session:
                assert (
                    await session.execute(text("SELECT count(*) FROM webhook_events"))
                ).scalar_one() == 0

    asyncio.run(scenario())


@pytest.mark.integration
def test_pg_unbound_payload_project_is_not_autolinked(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                response = await post(BODY.replace(b'"id":17', b'"id":999'))
            assert response.status_code == 400
            async with sessions() as session:
                assert (
                    await session.execute(text("SELECT count(*) FROM webhook_events"))
                ).scalar_one() == 0
                assert (
                    await session.execute(text("SELECT count(*) FROM repositories"))
                ).scalar_one() == 2

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("unsupported_event", [True, False])
def test_pg_unsupported_event_is_ignored_unknown_action_pending(
    database: Database, unsupported_event: bool
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            body = (
                b'{"object_kind":"system","event_name":"user_create"}'
                if unsupported_event
                else BODY.replace(b'"open"', b'"future_action"')
            )
            headers = {**HEADERS, "X-Gitlab-Event": "System Hook"} if unsupported_event else HEADERS
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                assert (await post(body, headers)).status_code == 202
            async with sessions() as session:
                row = (
                    (
                        await session.execute(
                            text(
                                (
                                    "SELECT projected_at,payload->'project' AS project,actio"
                                    "n FROM webhook_events"
                                )
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert (row["projected_at"] is not None) is unsupported_event
                if unsupported_event:
                    assert row["project"] is None
                    assert row["action"] is None
                else:
                    assert row["action"] == "future_action"
                for table in ("runs", "code_changes"):
                    assert (
                        await session.execute(text(f"SELECT count(*) FROM {table}"))
                    ).scalar_one() == 0
                assert (
                    await session.execute(text("SELECT count(*) FROM repositories"))
                ).scalar_one() == 2

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            (
                "UPDATE gitlab_connections SET revoked_at=now(),intake_e"
                "nabled=false WHERE provider_installation_id=:id"
            ),
            401,
        ),
        (
            "UPDATE gitlab_connections SET intake_enabled=false WHERE provider_installation_id=:id",
            503,
        ),
        ("UPDATE repositories SET enabled=false WHERE provider_installation_id=:id", 400),
    ],
)
def test_pg_auth_save_race_rolls_back_before_reauthentication(
    database: Database, change: str, expected: int
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            actual = build_gitlab_webhook_resources(sessions, intake_enabled=True)

            class RacingAuthenticator:
                def __init__(self) -> None:
                    self.calls = 0

                async def execute(
                    self, *, connection_id: UUID, secret: str | None
                ) -> GitLabWebhookAuthStatus:
                    self.calls += 1
                    if self.calls == 2:
                        # A fresh separate session can lock the table only once the
                        # failed admission UOW has fully exited and released locks.
                        async with sessions() as session:
                            await session.execute(
                                text("LOCK TABLE webhook_events IN ACCESS EXCLUSIVE MODE NOWAIT")
                            )
                            await session.execute(
                                text(
                                    "LOCK TABLE gitlab_connections IN ACCESS EXCLUSIVE MODE NOWAIT"
                                )
                            )
                    status = await actual.authenticate(connection_id=connection_id, secret=secret)
                    if self.calls == 1:
                        async with sessions() as session:
                            await session.execute(text(change), {"id": CONNECTION})
                            await session.commit()
                    return status

            class RealAcceptor:
                async def execute(
                    self, delivery: WebhookReceipt
                ) -> InstallationDeliveryDispatchStatus:
                    return await actual.accept(delivery)

            auth = RacingAuthenticator()
            with mounted(
                GitLabWebhookResources(
                    authenticator=auth, receiver=RealAcceptor(), intake_enabled=True
                )
            ):
                response = await post()
            assert response.status_code == expected
            assert auth.calls == 2
            async with sessions() as session:
                assert (
                    await session.execute(text("SELECT count(*) FROM webhook_events"))
                ).scalar_one() == 0

    asyncio.run(scenario())


@pytest.mark.integration
def test_pg_commit_failure_returns_safe_503_and_rolls_back(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            async with sessions() as session:
                await session.execute(
                    text(
                        "CREATE FUNCTION reject_webhook_commit() RETURNS trigger"
                        " LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'secret-i"
                        "nput-digest-DSN-canary'; END $$"
                    )
                )
                await session.execute(
                    text(
                        "CREATE CONSTRAINT TRIGGER reject_webhook_commit AFTER I"
                        "NSERT ON webhook_events DEFERRABLE INITIALLY DEFERRED F"
                        "OR EACH ROW EXECUTE FUNCTION reject_webhook_commit()"
                    )
                )
                await session.commit()
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                response = await post()
            assert response.status_code == 503
            assert response.json() == {"detail": "gitlab_webhook_unavailable"}
            assert "canary" not in response.text + caplog.text
            async with sessions() as session:
                assert (
                    await session.execute(text("SELECT count(*) FROM webhook_events"))
                ).scalar_one() == 0

    asyncio.run(scenario())


@pytest.mark.integration
def test_pg_auth_failure_returns_safe_503(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            async with sessions() as session:
                await session.execute(
                    text("ALTER TABLE gitlab_connections RENAME TO missing_auth_connection_canary")
                )
                await session.commit()
            with mounted(build_gitlab_webhook_resources(sessions, intake_enabled=True)):
                response = await post()
            assert response.status_code == 503
            assert response.json() == {"detail": "gitlab_webhook_unavailable"}
            assert "canary" not in response.text + caplog.text

    asyncio.run(scenario())


@pytest.mark.integration
def test_borrowed_pool_lifecycle_is_default_off_and_cleans_reference(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            isolated = FastAPI()
            async with gitlab_webhook_lifespan(isolated, sessions):
                resources = isolated.state.gitlab_webhook_resources
                assert isinstance(resources, GitLabWebhookResources)
                assert resources.intake_enabled is False
            assert not hasattr(isolated.state, "gitlab_webhook_resources")
            async with sessions() as session:
                assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
            async with gitlab_webhook_lifespan(isolated, None):
                assert not hasattr(isolated.state, "gitlab_webhook_resources")

    asyncio.run(scenario())


def test_public_dependency_override_composes_enabled_synthetic_resource() -> None:
    acceptor = Acceptor()
    resources = resources_for(Authenticator(), acceptor)
    app.dependency_overrides[get_gitlab_webhook_resources] = lambda: resources
    try:
        response = asyncio.run(post())
    finally:
        del app.dependency_overrides[get_gitlab_webhook_resources]
    assert response.status_code == 202
    assert len(acceptor.receipts) == 1


@pytest.mark.parametrize("literal", ["0e1000000000", "0e1073741823"])
def test_zero_exponent_control_within_pg_parser_limit(literal: str) -> None:
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(
            post(b'{"x":' + literal.encode() + b"}", {**HEADERS, "X-Gitlab-Event": "System Hook"})
        )
    assert response.status_code == 202


@pytest.mark.parametrize(
    "missing", ["object_kind", "project", "object_attributes", "id", "iid", "action"]
)
def test_missing_mr_identity(missing: str) -> None:
    payload = json.loads(BODY)
    target = (
        payload
        if missing in {"object_kind", "project", "object_attributes"}
        else payload["object_attributes"]
    )
    del target[missing]
    acceptor = Acceptor()
    with mounted(resources_for(Authenticator(), acceptor)):
        response = asyncio.run(post(json.dumps(payload).encode()))
    assert response.status_code == 400
    assert not acceptor.receipts
