"""Durable GitHub receipt and retryable projection boundaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from app.common.application.unit_of_work import UnitOfWork
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)

_LEASE = timedelta(minutes=5)
_DISPATCH_TIMEOUT_SECONDS = 240.0
_FAILURE_RETRY = timedelta(seconds=30)
_UNKNOWN_INSTALLATION_RETRY = timedelta(minutes=5)
_MAX_DISPATCH_ATTEMPTS = 3
# Finished receipts (projected, failed, or deferred and not revived since) are kept this long.
RECEIPT_RETENTION = timedelta(days=30)
_LOGGER = logging.getLogger(__name__)
_DEFERRED = frozenset(
    {
        InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION,
        InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY,
        InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
        InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS,
    }
)


@dataclass(frozen=True)
class WebhookReceipt:
    """Opaque JSON receipt; only transport and storage adapters decode it."""

    delivery_id: str
    event_name: str
    payload_json: str


class GitHubWebhookReceiptStore(Protocol):
    """Flush-only operations for the durable receipt and projection claim."""

    async def save(self, delivery: WebhookReceipt) -> bool: ...

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> WebhookReceipt | None: ...

    async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None: ...

    async def release(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        deferred_at: datetime,
        max_attempts: int,
    ) -> bool:
        """Retry a deferred delivery later; the last attempt sets projection_deferred_at (True).

        The receipt is then not selected until it is revived: by ``wake_receipts`` or, for
        installation events, by the hourly ``ReviveDeferredInstallationDeliveries``.
        """
        ...

    async def purge_finished(self, before: datetime) -> int: ...

    async def release_after_dispatch_failure(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        failed_at: datetime,
        max_attempts: int,
    ) -> None: ...

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]: ...


class GitHubWebhookReceiptUnitOfWork(UnitOfWork, Protocol):
    @property
    def receipts(self) -> GitHubWebhookReceiptStore: ...


class GitHubDeliveryDispatcher(Protocol):
    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult: ...


class ReceiveGitHubDelivery:
    """Commit each receipt and expose a replay sweep for a separate worker.

    Claim leases prevent concurrent projection. A process crash leaves the JSONB
    receipt reclaimable after the lease expires. Downstream projectors must be
    idempotent because a crash after their side effect can still cause replay.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], GitHubWebhookReceiptUnitOfWork],
        dispatcher: GitHubDeliveryDispatcher | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        dispatch_timeout_seconds: float = _DISPATCH_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < dispatch_timeout_seconds < _LEASE.total_seconds():
            raise ValueError("dispatch timeout must be shorter than claim lease")
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._now = now
        self._dispatch_timeout_seconds = dispatch_timeout_seconds

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
        async with self._uow_factory() as uow:
            inserted = await uow.receipts.save(delivery)
            await uow.commit()
        return (
            InstallationDeliveryDispatchStatus.PENDING
            if inserted
            else InstallationDeliveryDispatchStatus.DUPLICATE
        )

    async def replay_pending(self, *, limit: int = 100) -> int:
        """Project unclaimed/expired receipts; call from the webhook worker."""
        if self._dispatcher is None:
            raise RuntimeError("webhook dispatcher is not configured")
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._uow_factory() as uow:
            delivery_ids = await uow.receipts.pending_ids(self._now(), limit)
        projected = deferred = 0
        for delivery_id in delivery_ids:
            try:
                status = await self._project(delivery_id)
            except Exception:
                _LOGGER.exception("GitHub webhook projection failed for delivery %s", delivery_id)
                continue
            if status is not None:
                projected += 1
                deferred += status in _DEFERRED
        if delivery_ids:
            _LOGGER.info("GitHub webhook sweep: %d handled, %d deferred", projected, deferred)
        return projected

    async def purge_finished(self, retention: timedelta = RECEIPT_RETENTION) -> int:
        """Delete finished receipts older than ``retention`` (docs/WEBHOOK_WORKER.md)."""
        async with self._uow_factory() as uow:
            purged = await uow.receipts.purge_finished(self._now() - retention)
            await uow.commit()
        if purged:
            _LOGGER.info("Purged %d finished GitHub webhook receipts", purged)
        return purged

    async def _project(self, delivery_id: str) -> InstallationDeliveryDispatchStatus | None:
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("webhook dispatcher is not configured")
        token = uuid4()
        now = self._now()
        async with self._uow_factory() as uow:
            delivery = await uow.receipts.claim(delivery_id, token, now, now + _LEASE)
            if delivery is None:
                return None
            await uow.commit()

        try:
            result = await asyncio.wait_for(
                dispatcher.execute(delivery), timeout=self._dispatch_timeout_seconds
            )
        except Exception:
            async with self._uow_factory() as uow:
                failed_at = self._now()
                await uow.receipts.release_after_dispatch_failure(
                    delivery_id,
                    token,
                    failed_at + _FAILURE_RETRY,
                    failed_at,
                    _MAX_DISPATCH_ATTEMPTS,
                )
                await uow.commit()
            raise

        async with self._uow_factory() as uow:
            if result.status in _DEFERRED:
                now = self._now()
                final = await uow.receipts.release(
                    delivery_id,
                    token,
                    now + _UNKNOWN_INSTALLATION_RETRY,
                    now,
                    _MAX_DISPATCH_ATTEMPTS,
                )
                if final:
                    # Linking the installation wakes it up again (wake_receipts); installation
                    # events of linked installations received within the revival window are
                    # also revived hourly (ReviveDeferredInstallationDeliveries).
                    _LOGGER.warning(
                        "GitHub webhook delivery %s deferred after its last attempt: %s",
                        delivery_id,
                        result.status.value,
                    )
            else:
                await uow.receipts.mark_projected(delivery_id, token, self._now())
            await uow.commit()
        return result.status
