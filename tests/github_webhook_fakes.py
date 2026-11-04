"""Shared webhook receipt fakes for delivery tests."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from fastapi.testclient import TestClient

from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt

_SECRET = "receipt-test-secret"
_DELIVERY_LOGGER = "app.modules.integrations.webhooks.application.receive_github_delivery"


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

    async def fail_push_candidates(self, delivery_id: str, token: UUID, at: datetime) -> None:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.projection_failed_at = at
        row.claim_token = None
        row.lease_until = None
        row.retry_after = None

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

    async def retry_push_continuation(
        self, delivery_id: str, token: UUID, retry_after: datetime
    ) -> None:
        row = self.rows[delivery_id]
        assert row.claim_token == token
        row.claim_token = None
        row.lease_until = None
        row.projection_attempt_count = 0
        row.retry_after = retry_after

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
