"""Durable GitHub receipt and retryable projection boundaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from app.common.application.unit_of_work import UnitOfWork
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    VerifiedGitHubDelivery,
)

_LEASE = timedelta(minutes=5)
_DISPATCH_TIMEOUT_SECONDS = 240.0
_FAILURE_RETRY = timedelta(seconds=30)
_UNKNOWN_INSTALLATION_RETRY = timedelta(minutes=5)
_LOGGER = logging.getLogger(__name__)


class GitHubWebhookReceiptStore(Protocol):
    """Flush-only operations for the durable receipt and projection claim."""

    async def save(self, delivery: VerifiedGitHubDelivery) -> bool: ...

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> VerifiedGitHubDelivery | None: ...

    async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None: ...

    async def release(self, delivery_id: str, token: UUID, retry_after: datetime) -> None: ...

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]: ...


class GitHubWebhookReceiptUnitOfWork(UnitOfWork, Protocol):
    @property
    def receipts(self) -> GitHubWebhookReceiptStore: ...


class GitHubDeliveryDispatcher(Protocol):
    async def execute(
        self, delivery: VerifiedGitHubDelivery
    ) -> InstallationDeliveryDispatchResult: ...


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

    async def execute(self, delivery: VerifiedGitHubDelivery) -> InstallationDeliveryDispatchStatus:
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
        projected = 0
        for delivery_id in delivery_ids:
            try:
                status = await self._project(delivery_id)
            except Exception:
                _LOGGER.exception("GitHub webhook projection failed for delivery %s", delivery_id)
                continue
            if status is not None:
                projected += 1
        return projected

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
                await uow.receipts.release(delivery_id, token, self._now() + _FAILURE_RETRY)
                await uow.commit()
            raise

        async with self._uow_factory() as uow:
            if result.status in (
                InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION,
                InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY,
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
            ):
                await uow.receipts.release(
                    delivery_id, token, self._now() + _UNKNOWN_INSTALLATION_RETRY
                )
            else:
                await uow.receipts.mark_projected(delivery_id, token, self._now())
            await uow.commit()
        return result.status
