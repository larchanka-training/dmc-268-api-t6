"""Bitbucket webhook signatures are checked on the exact delivered bytes (#333)."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.modules.integrations.bitbucket.application.verify_webhook import (
    verify_bitbucket_signature,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)

INSTALLATION = UUID("00000000-0000-4000-8000-000000000331")
REPOSITORY = UUID("00000000-0000-4000-8000-000000000332")
REMOTE = UUID("00000000-0000-4000-8000-000000000333")
WORKSPACE = UUID("00000000-0000-4000-8000-000000000334")
SECRET = "a" * 40
BODY = b'{"repository":{"uuid":"{' + str(REMOTE).encode() + b'}"},"pullrequest":{"id":7}}'


@dataclass
class FakeWebhookResources:
    secret: str = SECRET
    authenticated_bodies: list[bytes] = field(default_factory=list)
    receipts: list[object] = field(default_factory=list)

    async def authenticate(
        self, *, installation_id: UUID, repository_id: UUID, body: bytes, signature: str | None
    ) -> object | None:
        self.authenticated_bodies.append(body)
        if not verify_bitbucket_signature(body, signature, self.secret):
            return None
        return SimpleNamespace(
            installation_id=installation_id,
            repository_id=repository_id,
            workspace_id=WORKSPACE,
            repository_uuid=REMOTE,
            binding_generation=1,
            webhook_generation=1,
        )

    async def accept(self, receipt: object) -> InstallationDeliveryDispatchStatus:
        self.receipts.append(receipt)
        return InstallationDeliveryDispatchStatus.PENDING


def _headers(body: bytes, *, signature: str | None = None) -> dict[str, str]:
    return {
        "X-Hub-Signature": signature
        or "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "X-Event-Key": "pullrequest:created",
        "X-Hook-UUID": "{" + str(uuid4()) + "}",
        "X-Request-UUID": "{" + str(uuid4()) + "}",
        "X-Attempt-Number": "1",
    }


def test_bitbucket_sha256_signature_matches_atlassian_vector_and_raw_bytes() -> None:
    secret = "It's a Secret to Everybody"
    payload = b"Hello World!"
    signature = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"
    assert verify_bitbucket_signature(payload, signature, secret)
    assert not verify_bitbucket_signature(payload + b" ", signature, secret)
    assert not verify_bitbucket_signature(payload, signature.upper(), secret)


def test_bitbucket_signature_rejects_malformed_or_missing_header() -> None:
    secret = "s" * 40
    payload = b'{"repository":{"uuid":"{00000000-0000-4000-8000-000000000333}"}}'
    correct = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_bitbucket_signature(payload, correct, secret)
    for malformed in (None, "", correct + "x", "sha1=" + correct[7:], "sha256=nothex"):
        assert not verify_bitbucket_signature(payload, malformed, secret)


def test_signed_route_receives_exact_bytes_and_rejects_bad_signature_before_json() -> None:
    resources = FakeWebhookResources()
    app.state.bitbucket_webhook_resources = resources
    try:
        client = TestClient(app)
        route = f"/webhooks/bitbucket/{INSTALLATION}/{REPOSITORY}"
        accepted = client.post(route, content=BODY, headers=_headers(BODY))
        assert accepted.status_code == 202
        assert accepted.json() == {"status": "pending"}
        assert resources.authenticated_bodies == [BODY]
        assert len(resources.receipts) == 1

        invalid = client.post(route, content=b"invalid-json-canary", headers=_headers(BODY))
        assert invalid.status_code == 401
        assert len(resources.receipts) == 1
        assert "canary" not in invalid.text
    finally:
        del app.state.bitbucket_webhook_resources


def test_webhook_bounds_headers_and_payload_are_rejected_without_receipt() -> None:
    resources = FakeWebhookResources()
    app.state.bitbucket_webhook_resources = resources
    route = f"/webhooks/bitbucket/{INSTALLATION}/{REPOSITORY}"
    try:
        client = TestClient(app)
        oversized = client.post(route, content=b"x" * (256 * 1024 + 1), headers=_headers(BODY))
        assert oversized.status_code == 413
        assert resources.authenticated_bodies == []
        absent = _headers(BODY)
        del absent["X-Hub-Signature"]
        assert client.post(route, content=BODY, headers=absent).status_code == 401
        malformed_json = b"not-json"
        assert (
            client.post(route, content=malformed_json, headers=_headers(malformed_json)).status_code
            == 400
        )
        wrong_repository = BODY.replace(str(REMOTE).encode(), str(uuid4()).encode())
        assert (
            client.post(
                route, content=wrong_repository, headers=_headers(wrong_repository)
            ).status_code
            == 400
        )
        assert (
            client.post(
                route, content=BODY, headers={**_headers(BODY), "X-Attempt-Number": "0"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                route,
                content=BODY,
                headers={**_headers(BODY), "X-Attempt-Number": "1" * 5000},
            ).status_code
            == 400
        )
        for invalid_json_string in (
            BODY[:-1] + b',"note":"\\u0000"}',
            BODY[:-1] + b',"note":"\\ud800"}',
        ):
            assert (
                client.post(
                    route,
                    content=invalid_json_string,
                    headers=_headers(invalid_json_string),
                ).status_code
                == 400
            )
        duplicated = list(_headers(BODY).items())
        duplicated.append(("X-Event-Key", "repo:push"))
        assert client.post(route, content=BODY, headers=duplicated).status_code == 400
        assert resources.receipts == []
    finally:
        del app.state.bitbucket_webhook_resources


def test_deep_signed_json_returns_bad_request_instead_of_server_error() -> None:
    resources = FakeWebhookResources()
    app.state.bitbucket_webhook_resources = resources
    route = f"/webhooks/bitbucket/{INSTALLATION}/{REPOSITORY}"
    deep = (
        b'{"repository":{"uuid":"{'
        + str(REMOTE).encode()
        + b'}"},"nested":'
        + b"[" * 500
        + b"0"
        + b"]" * 500
        + b"}"
    )
    try:
        response = TestClient(app).post(route, content=deep, headers=_headers(deep))
        assert response.status_code == 400
        assert resources.receipts == []
    finally:
        del app.state.bitbucket_webhook_resources
