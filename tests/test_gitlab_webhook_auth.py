"""Public authentication decisions using application-owned fake ports."""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.gitlab import (
    GitLabWebhookResources,
    GitLabWebhookUnavailable,
    build_gitlab_webhook_resources,
)
from app.modules.integrations.gitlab.application.authenticate_webhook import (
    AuthenticateGitLabWebhook,
    GitLabWebhookAuthRecord,
    GitLabWebhookAuthStatus,
)
from app.modules.integrations.gitlab.application.webhook_receipts import (
    GitLabReceiptError,
    prepare_gitlab_receipt,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt

CONNECTION = UUID("00000000-0000-0000-0000-000000000071")
OTHER = UUID("00000000-0000-0000-0000-000000000072")
SECRET = "synthetic-webhook-secret-canary-0001"
DIGEST = "a" * 64


@dataclass
class Reader:
    record: GitLabWebhookAuthRecord | None

    async def read(self, connection_id: UUID) -> GitLabWebhookAuthRecord | None:
        return self.record


class Verifier:
    def verify(self, secret: str | None, expected_digest: str) -> bool:
        return secret == SECRET and expected_digest == DIGEST


def test_authenticates_exact_connection_with_valid_secret() -> None:
    auth = AuthenticateGitLabWebhook(
        reader=Reader(GitLabWebhookAuthRecord(CONNECTION, DIGEST, True, True)),
        verifier=Verifier(),
    )
    assert asyncio.run(auth.execute(connection_id=CONNECTION, secret=SECRET)) == (
        GitLabWebhookAuthStatus.AUTHENTICATED
    )


@pytest.mark.parametrize(
    ("record", "secret", "expected"),
    [
        (None, SECRET, GitLabWebhookAuthStatus.INVALID_CREDENTIALS),
        (
            GitLabWebhookAuthRecord(CONNECTION, DIGEST, True, True),
            None,
            GitLabWebhookAuthStatus.INVALID_CREDENTIALS,
        ),
        (
            GitLabWebhookAuthRecord(CONNECTION, DIGEST, True, True),
            "wrong",
            GitLabWebhookAuthStatus.INVALID_CREDENTIALS,
        ),
        (
            GitLabWebhookAuthRecord(CONNECTION, DIGEST, False, True),
            SECRET,
            GitLabWebhookAuthStatus.INVALID_CREDENTIALS,
        ),
        (
            GitLabWebhookAuthRecord(OTHER, DIGEST, True, True),
            SECRET,
            GitLabWebhookAuthStatus.INVALID_CREDENTIALS,
        ),
        (
            GitLabWebhookAuthRecord(CONNECTION, DIGEST, True, False),
            "wrong",
            GitLabWebhookAuthStatus.INVALID_CREDENTIALS,
        ),
        (
            GitLabWebhookAuthRecord(CONNECTION, DIGEST, False, False),
            SECRET,
            GitLabWebhookAuthStatus.INVALID_CREDENTIALS,
        ),
        (
            GitLabWebhookAuthRecord(CONNECTION, DIGEST, True, False),
            SECRET,
            GitLabWebhookAuthStatus.DISABLED,
        ),
    ],
)
def test_credentials_precede_intake_disclosure(
    record: GitLabWebhookAuthRecord | None,
    secret: str | None,
    expected: GitLabWebhookAuthStatus,
) -> None:
    auth = AuthenticateGitLabWebhook(reader=Reader(record), verifier=Verifier())
    assert asyncio.run(auth.execute(connection_id=CONNECTION, secret=secret)) == expected


def test_record_repr_hides_digest() -> None:
    assert DIGEST not in repr(GitLabWebhookAuthRecord(CONNECTION, DIGEST, True, True))


def test_default_resources_refuse_operations_without_db_or_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
        "SSL_CERT_FILE",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, "invalid-config-canary")
    resources = build_gitlab_webhook_resources(async_sessionmaker[AsyncSession]())
    assert resources.intake_enabled is False
    delivery = prepare_gitlab_receipt(
        connection_id=CONNECTION, webhook_id="event-1", event_name="Push Hook", payload_json="{}"
    )
    with pytest.raises(GitLabWebhookUnavailable, match="^gitlab_webhook_unavailable$"):
        asyncio.run(resources.authenticate(connection_id=CONNECTION, secret=SECRET))
    with pytest.raises(GitLabWebhookUnavailable, match="^gitlab_webhook_unavailable$"):
        asyncio.run(resources.accept(delivery))


@dataclass
class ResourceAuthenticator:
    status: GitLabWebhookAuthStatus = GitLabWebhookAuthStatus.AUTHENTICATED
    failure: Exception | None = field(default=None, repr=False)
    calls: list[UUID] = field(default_factory=list)

    async def execute(self, *, connection_id: UUID, secret: str | None) -> GitLabWebhookAuthStatus:
        self.calls.append(connection_id)
        if self.failure is not None:
            raise self.failure
        return self.status


@dataclass
class ResourceAcceptor:
    status: InstallationDeliveryDispatchStatus = InstallationDeliveryDispatchStatus.PENDING
    failure: Exception | None = field(default=None, repr=False)
    calls: list[str] = field(default_factory=list)

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
        self.calls.append(delivery.delivery_id)
        if self.failure is not None:
            raise self.failure
        return self.status


def test_disabled_resources_never_invoke_authentication_or_acceptance() -> None:
    auth = ResourceAuthenticator()
    acceptor = ResourceAcceptor()
    resources = GitLabWebhookResources(authenticator=auth, receiver=acceptor)
    with pytest.raises(GitLabWebhookUnavailable):
        asyncio.run(resources.authenticate(connection_id=CONNECTION, secret=SECRET))
    with pytest.raises(GitLabWebhookUnavailable):
        asyncio.run(resources.accept(WebhookReceipt("event", "Push Hook", "{}")))
    assert auth.calls == []
    assert acceptor.calls == []


@pytest.mark.parametrize("operation", ["authenticate", "accept"])
def test_resource_boundary_suppresses_raw_failure_chains(
    operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = SECRET + DIGEST + "postgresql://dsn-secret-canary"
    cause = ValueError(canary)
    failure = RuntimeError(canary)
    failure.__cause__ = cause
    resources = GitLabWebhookResources(
        authenticator=ResourceAuthenticator(failure=failure),
        receiver=ResourceAcceptor(failure=failure),
        intake_enabled=True,
    )
    with pytest.raises(GitLabWebhookUnavailable) as raised:
        if operation == "authenticate":
            asyncio.run(resources.authenticate(connection_id=CONNECTION, secret=SECRET))
        else:
            asyncio.run(resources.accept(WebhookReceipt("event", "Push Hook", "{}")))
    assert str(raised.value) == "gitlab_webhook_unavailable"
    rendered = "".join(traceback.format_exception(raised.value))
    for protected in (SECRET, DIGEST, "postgresql://dsn-secret-canary"):
        assert protected not in rendered
        assert protected not in repr(resources)
        assert protected not in caplog.text
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    [record] = [entry for entry in caplog.records if entry.name == "app.bootstrap.gitlab"]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f"GitLab webhook {operation} failed"
    assert record.__dict__["error_type"] == "RuntimeError"
    assert record.exc_info is None and record.stack_info is None


def test_resource_boundary_preserves_expected_safe_admission_failure() -> None:
    resources = GitLabWebhookResources(
        authenticator=ResourceAuthenticator(),
        receiver=ResourceAcceptor(failure=GitLabReceiptError("delivery_conflict")),
        intake_enabled=True,
    )
    with pytest.raises(GitLabReceiptError, match="^delivery_conflict$"):
        asyncio.run(resources.accept(WebhookReceipt("event", "Push Hook", "{}")))
