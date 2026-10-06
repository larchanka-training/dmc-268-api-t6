"""Autonomous revival of deferred installation deliveries (docs/WEBHOOK_WORKER.md)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.common.application.unit_of_work import UnitOfWork

# Tech-lead-approved bounds (api#71): a receipt deferred at least 45 minutes ago and received
# within the last seven days; a longer GitHub outage is not a transient failure. Its three
# attempts take about ten minutes, so the next hourly tick revives it again: about hourly.
REVIVAL_DELAY = timedelta(minutes=45)
REVIVAL_WINDOW = timedelta(days=7)
_LOGGER = logging.getLogger(__name__)


class DeferredInstallationDeliveryStore(Protocol):
    """Flush-only reset of deferred installation-event receipts."""

    async def revive_deferred_installation_deliveries(
        self, *, deferred_before: datetime, received_after: datetime
    ) -> int:
        """Give matching receipts of linked installations a fresh attempt budget; count them."""
        ...


class DeferredInstallationDeliveryUnitOfWork(UnitOfWork, Protocol):
    @property
    def receipts(self) -> DeferredInstallationDeliveryStore: ...


class ReviveDeferredInstallationDeliveries:
    """Revive installation events deferred after their last attempt; the worker runs it hourly.

    Only installation events of linked installations qualify, so a GitHub outage that
    outlasted three attempts heals without a login (``wake_receipts``).
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], DeferredInstallationDeliveryUnitOfWork],
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now

    async def execute(self) -> int:
        now = self._now()
        async with self._uow_factory() as uow:
            revived = await uow.receipts.revive_deferred_installation_deliveries(
                deferred_before=now - REVIVAL_DELAY,
                received_after=now - REVIVAL_WINDOW,
            )
            await uow.commit()
        if revived:
            _LOGGER.info("Revived %d deferred GitHub installation deliveries", revived)
        return revived
