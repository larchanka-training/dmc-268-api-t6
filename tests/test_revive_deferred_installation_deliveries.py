"""The hourly revival use case against fakes of its ports (api#71)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest

from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
_USE_CASE_LOGGER = (
    "app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries"
)


@dataclass
class Store:
    revived: int
    calls: list[tuple[datetime, datetime]] = field(default_factory=list)

    async def revive_deferred_installation_deliveries(
        self, *, deferred_before: datetime, received_after: datetime
    ) -> int:
        self.calls.append((deferred_before, received_after))
        return self.revived


@dataclass
class UnitOfWork:
    receipts: Store
    commits: int = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        raise AssertionError("the revival never rolls back explicitly")


def _revive(uow: UnitOfWork) -> int:
    return asyncio.run(
        ReviveDeferredInstallationDeliveries(uow_factory=lambda: uow, now=lambda: NOW).execute()
    )


def test_revival_asks_for_receipts_deferred_45_minutes_and_received_7_days_before_now(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uow = UnitOfWork(Store(revived=2))

    with caplog.at_level(logging.INFO, logger=_USE_CASE_LOGGER):
        revived = _revive(uow)

    assert revived == 2
    assert uow.receipts.calls == [
        (datetime(2026, 10, 6, 11, 15, tzinfo=UTC), datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    ]
    assert uow.commits == 1
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == _USE_CASE_LOGGER
    ] == [(logging.INFO, "Revived 2 deferred GitHub installation deliveries")]


def test_a_revival_that_finds_nothing_commits_once_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uow = UnitOfWork(Store(revived=0))

    with caplog.at_level(logging.INFO, logger=_USE_CASE_LOGGER):
        revived = _revive(uow)

    assert revived == 0
    assert uow.commits == 1
    assert [record for record in caplog.records if record.name == _USE_CASE_LOGGER] == []
