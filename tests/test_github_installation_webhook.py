"""HTTP boundary for verified GitHub installation deliveries."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from app.main import (
    _has_valid_github_signature,
    app,
    get_github_installation_delivery_dispatcher,
    get_github_webhook_secret,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    VerifiedGitHubDelivery,
)

_SECRET = "webhook-test-secret"


@dataclass
class FakeDispatcher:
    result: InstallationDeliveryDispatchResult
    deliveries: list[VerifiedGitHubDelivery] = field(default_factory=list)

    async def execute(self, delivery: VerifiedGitHubDelivery) -> InstallationDeliveryDispatchResult:
        self.deliveries.append(delivery)
        return self.result


def _signed_headers(
    raw_body: bytes, *, event_name: str = "installation_repositories"
) -> dict[str, str]:
    digest = hmac.new(_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event_name,
        "X-GitHub-Delivery": "delivery-42",
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def _post(dispatcher: FakeDispatcher, raw_body: bytes, headers: dict[str, str]) -> Any:
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_installation_delivery_dispatcher] = lambda: dispatcher
    try:
        return TestClient(app).post("/webhooks/github", content=raw_body, headers=headers)
    finally:
        app.dependency_overrides.clear()


def test_signed_delivery_is_dispatched_and_acknowledged() -> None:
    raw_body = json.dumps({"action": "added", "installation": {"id": 17}}).encode()
    dispatcher = FakeDispatcher(
        InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)
    )

    response = _post(dispatcher, raw_body, _signed_headers(raw_body))

    assert response.status_code == 202
    assert response.json() == {"status": "onboarded"}
    assert dispatcher.deliveries == [
        VerifiedGitHubDelivery(
            delivery_id="delivery-42",
            event_name="installation_repositories",
            payload={"action": "added", "installation": {"id": 17}},
        )
    ]


def test_invalid_signature_is_rejected_without_dispatch() -> None:
    raw_body = b'{"action":"added"}'
    dispatcher = FakeDispatcher(
        InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)
    )
    headers = _signed_headers(raw_body)
    headers["X-Hub-Signature-256"] = "sha256=not-a-valid-signature"

    response = _post(dispatcher, raw_body, headers)

    assert response.status_code == 401
    assert dispatcher.deliveries == []


def test_non_ascii_or_non_hex_signature_is_rejected_without_dispatch() -> None:
    raw_body = b'{"action":"added"}'
    dispatcher = FakeDispatcher(
        InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)
    )

    for digest in ("z" * 64, "a" * 63):
        headers = _signed_headers(raw_body)
        headers["X-Hub-Signature-256"] = f"sha256={digest}"

        response = _post(dispatcher, raw_body, headers)

        assert response.status_code == 401
    assert not _has_valid_github_signature(
        raw_body=raw_body, signature=f"sha256={'é' * 64}", secret=_SECRET
    )
    assert dispatcher.deliveries == []


def test_supported_but_invalid_delivery_is_acknowledged_without_dispatch() -> None:
    raw_body = b"not json"
    dispatcher = FakeDispatcher(
        InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)
    )

    response = _post(dispatcher, raw_body, _signed_headers(raw_body))

    assert response.status_code == 202
    assert response.json() == {"status": "ignored_invalid_event"}
    assert dispatcher.deliveries == []


def test_unsupported_delivery_is_acknowledged_by_dispatch_contract() -> None:
    raw_body = b"{}"
    dispatcher = FakeDispatcher(
        InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.IGNORED_INVALID_EVENT)
    )

    response = _post(dispatcher, raw_body, _signed_headers(raw_body, event_name="push"))

    assert response.status_code == 202
    assert response.json() == {"status": "ignored_invalid_event"}
    assert dispatcher.deliveries[0].event_name == "push"


def test_unknown_installation_is_acknowledged_by_dispatch_contract() -> None:
    raw_body = b'{"action":"added","installation":{"id":17}}'
    dispatcher = FakeDispatcher(
        InstallationDeliveryDispatchResult(
            InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION
        )
    )

    response = _post(dispatcher, raw_body, _signed_headers(raw_body))

    assert response.status_code == 202
    assert response.json() == {"status": "ignored_unknown_installation"}
    assert len(dispatcher.deliveries) == 1
