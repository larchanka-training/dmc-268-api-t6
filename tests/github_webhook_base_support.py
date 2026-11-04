"""Verified GitHub webhook receipts are durable before projection."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.types import Message, Scope

from alembic import command
from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import (
    _has_valid_github_signature,
    _is_github_delivery_id,
    app,
    get_github_webhook_secret,
)
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter, action_of
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application import receive_github_delivery as receipt_engine
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    GitHubInstallationDeliveryDispatcher,
    GitHubInstallationResolver,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    InstallationOnboardingHandler,
    PullRequestLabelIntentHandler,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    FailureCategory,
    ReceiveGitHubDelivery,
    ReplayBatchResult,
    WebhookReceipt,
)
from app.modules.integrations.webhooks.infrastructure.failure_category import classify_failure
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestProjectionStatus,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent, RunTriggerOutcome

_SECRET = "receipt-test-secret"


@dataclass
class FakeDispatcher:
    deliveries: list[WebhookReceipt] = field(default_factory=list)

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        self.deliveries.append(delivery)
        return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)


@dataclass
class FakeClaimedReceipt:
    delivery: WebhookReceipt
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    projected: bool = False
    projection_attempt_count: int = 0
    projection_failed_at: datetime | None = None
    projection_deferred_at: datetime | None = None
    claim_token: object | None = None
    lease_until: datetime | None = None
    retry_after: datetime | None = None


@dataclass
class FakeReceiptUnitOfWork:
    rows: dict[str, FakeClaimedReceipt] = field(default_factory=dict)
    commits: int = 0

    @property
    def receipts(self) -> FakeReceiptUnitOfWork:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def save(self, delivery: WebhookReceipt) -> bool:
        if delivery.delivery_id in self.rows:
            return False
        self.rows[delivery.delivery_id] = FakeClaimedReceipt(delivery)
        return True

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> WebhookReceipt | None:
        row = self.rows[delivery_id]
        if (
            row.projected
            or row.projection_failed_at is not None
            or row.projection_deferred_at is not None
            or (row.lease_until is not None and row.lease_until > now)
        ):
            return None
        if row.retry_after is not None and row.retry_after > now:
            return None
        row.claim_token = token
        row.lease_until = until
        return row.delivery

    async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.projected = True
        row.claim_token = None
        row.lease_until = None

    async def retry_run_trigger(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        *,
        now: datetime,
        received_before: datetime,
    ) -> bool:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.claim_token = None
        row.lease_until = None
        row.projection_attempt_count = 0
        expired = row.received_at <= received_before
        row.projection_failed_at = now if expired else None
        row.retry_after = None if expired else retry_after
        return expired

    async def release(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        deferred_at: datetime,
        max_attempts: int,
    ) -> bool:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.projection_attempt_count += 1
        row.claim_token = None
        row.lease_until = None
        if row.projection_attempt_count >= max_attempts:
            row.projection_deferred_at = deferred_at
            row.retry_after = None
            return True
        row.retry_after = retry_after
        return False

    async def purge_finished(self, before: datetime) -> int:
        return 0

    async def release_after_dispatch_failure(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        failed_at: datetime,
        max_attempts: int,
    ) -> bool:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.projection_attempt_count += 1
        row.claim_token = None
        row.lease_until = None
        if row.projection_attempt_count == max_attempts:
            row.projection_failed_at = failed_at
            row.retry_after = None
            return True
        row.retry_after = retry_after
        return False

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        return tuple(
            delivery_id
            for delivery_id, row in self.rows.items()
            if not row.projected
            and row.projection_failed_at is None
            and row.projection_deferred_at is None
            and (row.lease_until is None or row.lease_until <= now)
            and (row.retry_after is None or row.retry_after <= now)
        )[:limit]


def _outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("GitHub webhook delivery ")
    ]


_DELIVERY_LOGGER = "app.modules.integrations.webhooks.application.receive_github_delivery"
_SECRET_IN_MESSAGE = "ghs_t72_sentinel"
_NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _pull_request_receipt(delivery_id: str, action: str = "labeled") -> WebhookReceipt:
    payload: dict[str, object] = {
        "action": action,
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Review parser",
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
            "state": "open",
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }
    if action in {"labeled", "unlabeled"}:
        payload["label"] = {"name": "ai-review"}
    return VerifiedGitHubDelivery(delivery_id, "pull_request", payload).to_receipt()


@dataclass
class FailingTypedDispatcher:
    """The typed dispatcher behind the real adapter: it raises ``error`` or hangs."""

    error: BaseException | None = None

    async def execute(self, delivery: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
        if self.error is not None:
            raise self.error
        await asyncio.sleep(10)
        raise AssertionError("the dispatch timeout did not cancel the dispatcher")


def _failure_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("GitHub webhook delivery ")
        and " failed stage=" in record.getMessage()
    ]


def _sweep_failure_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.ERROR]


def _fallback_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and " failure line: " in record.getMessage()
    ]


def _dispatch_failure_run(
    receipt: WebhookReceipt,
    typed: FailingTypedDispatcher,
    caplog: pytest.LogCaptureFixture,
    *,
    dispatch_timeout_seconds: float = 1.0,
    reader: Callable[[WebhookReceipt], str | None] = action_of,
    classifier: Callable[[BaseException], FailureCategory] | None = classify_failure,
) -> FakeReceiptUnitOfWork:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(typed),
        now=lambda: _NOW,
        dispatch_timeout_seconds=dispatch_timeout_seconds,
        action_of=reader,
        classify_failure=classifier,
    )
    asyncio.run(receiver.execute(receipt))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0
    return uow


def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://api.github.test/pulls/7?t={_SECRET_IN_MESSAGE}")
    return httpx.HTTPStatusError(
        f"Server error for {_SECRET_IN_MESSAGE}", request=request, response=httpx.Response(500)
    )


def _warning_and_error_records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == _DELIVERY_LOGGER and record.levelno >= logging.WARNING
    ]


_PAYLOAD_SENTINEL = "SENTINEL-t72-payload-value"
_DISPATCH_LOGGER = "app.modules.integrations.webhooks.api.dispatch"


class UnreachableTypedDispatcher:
    """The typed dispatcher behind the real adapter: an invalid payload never reaches it."""

    async def execute(self, delivery: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
        raise AssertionError("an invalid payload reached typed dispatch")


def _replay_invalid_receipt(
    receipt: WebhookReceipt, caplog: pytest.LogCaptureFixture
) -> FakeReceiptUnitOfWork:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(UnreachableTypedDispatcher()),
        now=lambda: _NOW,
    )
    asyncio.run(receiver.execute(receipt))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 1
    return uow


def _synchronize_with_unparsable_number() -> str:
    """A ``synchronize`` payload that is valid except for a string ``pull_request.number``."""
    payload = json.loads(_pull_request_receipt("payload", "synchronize").payload_json)
    payload["pull_request"]["number"] = _PAYLOAD_SENTINEL
    return json.dumps(payload)


def _pull_request_payload(action: str = "labeled") -> dict[str, object]:
    payload = json.loads(_pull_request_receipt("payload", action).payload_json)
    assert isinstance(payload, dict)
    return payload


def _without_label() -> dict[str, object]:
    payload = _pull_request_payload()
    del payload["label"]
    return payload


def _mismatched_number() -> dict[str, object]:
    return {**_pull_request_payload(), "number": 8}


_EVENT_NAMES_AS_LOGGED = [
    ("pull_request", "pull_request"),
    ("pull_request\nx", "-"),
    ("Pull Request", "-"),
]


def _headers(raw_body: bytes, *, delivery_id: str = "delivery-42") -> dict[str, str]:
    digest = hmac.new(_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": "installation_repositories",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def _post(
    raw_body: bytes,
    headers: Mapping[str, str],
    receipts: FakeReceiptUnitOfWork,
) -> tuple[int, dict[str, str]]:
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: lambda: receipts
    try:
        response = TestClient(app).post("/webhooks/github", content=raw_body, headers=headers)
        return response.status_code, response.json()
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def isolated_webhook_database() -> Iterator[tuple[Connection, str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_webhook_receipts_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection, database_url, schema
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


__all__ = (
    "CiTriggerEvent",
    "Config",
    "Connection",
    "DatabaseError",
    "FailingTypedDispatcher",
    "FailureCategory",
    "FakeClaimedReceipt",
    "FakeDispatcher",
    "FakeReceiptUnitOfWork",
    "FrozenInstanceError",
    "GitHubInstallationDeliveryDispatcher",
    "GitHubInstallationResolver",
    "GitHubWebhookDispatchAdapter",
    "InstallationDeliveryDispatchResult",
    "InstallationDeliveryDispatchStatus",
    "InstallationOnboardingHandler",
    "Mapping",
    "Message",
    "OperationalError",
    "PoolTimeoutError",
    "PullRequestEvent",
    "PullRequestLabelEvent",
    "PullRequestLabelIntentHandler",
    "PullRequestProjectionStatus",
    "ReceiveGitHubDelivery",
    "ReplayBatchResult",
    "RunTriggerOutcome",
    "Scope",
    "SqlAlchemyGitHubWebhookReceiptUnitOfWork",
    "TestClient",
    "TracebackType",
    "UTC",
    "UUID",
    "ValidationError",
    "VerifiedGitHubDelivery",
    "WebhookReceipt",
    "_DELIVERY_LOGGER",
    "_DISPATCH_LOGGER",
    "_EVENT_NAMES_AS_LOGGED",
    "_NOW",
    "_PAYLOAD_SENTINEL",
    "_SECRET",
    "_SECRET_IN_MESSAGE",
    "_dispatch_failure_run",
    "_failure_lines",
    "_fallback_records",
    "_has_valid_github_signature",
    "_headers",
    "_http_status_error",
    "_is_github_delivery_id",
    "_mismatched_number",
    "_outcome_lines",
    "_post",
    "_pull_request_receipt",
    "_replay_invalid_receipt",
    "_sweep_failure_records",
    "_synchronize_with_unparsable_number",
    "_warning_and_error_records",
    "_without_label",
    "action_of",
    "app",
    "async_sessionmaker",
    "asyncio",
    "cast",
    "classify_failure",
    "command",
    "create_async_engine",
    "dataclass",
    "datetime",
    "field",
    "get_github_webhook_receipt_uow_factory",
    "get_github_webhook_secret",
    "httpx",
    "isolated_webhook_database",
    "json",
    "logging",
    "psycopg",
    "pytest",
    "receipt_engine",
    "text",
    "timedelta",
    "uuid4",
)
