"""Unreadable repository details defer the receipt; an hourly revival or login revives it (api#71).

The real receipt store, ``ReceiveGitHubDelivery``, dispatcher, projector and details
provider run against a migrated schema; only GitHub is an ``httpx.MockTransport``.

Opt-in with ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)
from app.modules.workspaces.infrastructure.models import Workspace
from tests.github_webhook_fixtures import load_github_webhook_fixture

START = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
INSTALLATION_EXTERNAL_ID = 1000001
FULL_NAME = "example-owner/example-repo-two"
REPOSITORY_PATH = f"/repos/{FULL_NAME}"
_RECEIVER_LOGGER = "app.modules.integrations.webhooks.application.receive_github_delivery"


@dataclass(frozen=True)
class Database:
    url: str
    schema: str


@pytest.fixture
def database() -> Iterator[Database]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_details_receipts_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            yield Database(database_url, schema)
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@dataclass
class GitHub:
    """GitHub as the worker sees it; the ``GET /repos`` answer changes between sweeps."""

    details_status: int = 404
    details_body: dict[str, object] = field(default_factory=lambda: {"message": "Not Found"})
    requests: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == REPOSITORY_PATH:
            return httpx.Response(self.details_status, json=self.details_body)
        if request.method == "GET" and request.url.path == f"{REPOSITORY_PATH}/git/trees/trunk":
            return httpx.Response(200, json={"tree": [{"path": "src/app.ts", "type": "blob"}]})
        if request.method == "POST" and request.url.path == f"{REPOSITORY_PATH}/labels":
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"message": "unexpected request"})

    def answer_with_details(self) -> None:
        self.details_status = 200
        self.details_body = {
            "default_branch": "trunk",
            "html_url": f"https://example.test/{FULL_NAME}",
        }

    def repository_reads(self) -> int:
        return self.requests.count(("GET", REPOSITORY_PATH))


class TokenProvider:
    async def get_installation_access_token(self, installation_external_id: int) -> str:
        assert installation_external_id == INSTALLATION_EXTERNAL_ID
        return "test-installation-token"


@dataclass(frozen=True)
class Receipt:
    attempts: int
    retry_after: bool
    deferred: bool
    failed: bool
    projected: bool


@dataclass
class Worker:
    factory: async_sessionmaker[AsyncSession]
    clock: list[datetime]
    receiver: ReceiveGitHubDelivery
    reviver: ReviveDeferredInstallationDeliveries

    async def receive(self) -> None:
        await self.receiver.execute(
            VerifiedGitHubDelivery(
                "delivery-details",
                "installation_repositories",
                load_github_webhook_fixture("installation_repositories_added"),
            ).to_receipt()
        )

    async def received_at(self, at: datetime) -> None:
        """Pin the database-assigned receipt time to the test's clock."""
        async with self.factory() as session:
            await session.execute(text("UPDATE webhook_events SET received_at = :at"), {"at": at})
            await session.commit()

    async def revive(self) -> int:
        """The worker's hourly revival tick, at the current clock."""
        return await self.reviver.execute()

    async def sweep(self) -> int:
        """One replay sweep, then six minutes pass: longer than either retry delay."""
        handled = await self.receiver.replay_pending()
        self.clock[0] += timedelta(minutes=6)
        return handled

    async def wake(self) -> None:
        async with SqlAlchemyGitHubInstallationLinkUnitOfWork(self.factory) as uow:
            await uow.links.wake_receipts(INSTALLATION_EXTERNAL_ID)
            await uow.commit()

    async def receipt(self) -> Receipt:
        async with self.factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT projection_attempt_count, retry_after IS NOT NULL, "
                        "projection_deferred_at IS NOT NULL, "
                        "projection_failed_at IS NOT NULL, projected_at IS NOT NULL "
                        "FROM webhook_events"
                    )
                )
            ).one()
        return Receipt(*row)

    async def repositories(self) -> list[tuple[str, str]]:
        async with self.factory() as session:
            rows = (await session.scalars(select(Repository))).all()
        return [(row.default_branch, row.web_url) for row in rows]


@asynccontextmanager
async def _worker(database: Database, github: GitHub) -> AsyncIterator[Worker]:
    engine = create_async_engine(
        database.url,
        connect_args={"options": f"-csearch_path={database.schema}"},
        poolclass=NullPool,
    )
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(github), base_url="https://api.github.com"
    )
    try:
        async with factory() as session:
            workspace = Workspace(id=uuid4(), name="details", daily_budget_usd=Decimal("1"))
            session.add(workspace)
            await session.flush()
            session.add(
                ProviderInstallation(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    provider="github",
                    external_id=INSTALLATION_EXTERNAL_ID,
                    provider_metadata={},
                )
            )
            await session.commit()
        clock = [START]
        resources = ReviewsApiResources(engine, factory)
        dispatcher = resources.github_installation_delivery_dispatcher(
            client=client, token_provider=TokenProvider()
        )
        yield Worker(
            factory,
            clock,
            ReceiveGitHubDelivery(
                uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory),
                dispatcher=dispatcher,
                now=lambda: clock[0],
            ),
            # Composed as app.webhook_worker does, on the test's clock.
            ReviveDeferredInstallationDeliveries(
                uow_factory=resources.github_webhook_receipts, now=lambda: clock[0]
            ),
        )
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.integration
def test_unreadable_details_are_deferred_after_three_attempts_and_a_login_revives_the_receipt(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """A 404 on ``GET /repos`` survives attempt exhaustion; the next login gives it a retry."""
    github = GitHub()

    async def scenario() -> dict[str, object]:
        seen: dict[str, object] = {}
        async with _worker(database, github) as worker:
            await worker.receive()
            for step in (1, 2, 3):
                seen[f"handled_{step}"] = await worker.sweep()
                seen[f"receipt_{step}"] = await worker.receipt()
            seen["rows_after_exhaustion"] = await worker.repositories()
            reads_after_exhaustion = github.repository_reads()

            seen["handled_extra"] = await worker.sweep()
            seen["receipt_extra"] = await worker.receipt()
            seen["extra_reads"] = github.repository_reads() - reads_after_exhaustion

            await worker.wake()
            seen["receipt_woken"] = await worker.receipt()

            github.answer_with_details()
            seen["handled_after_wake"] = await worker.sweep()
            seen["receipt_final"] = await worker.receipt()
            seen["rows_final"] = await worker.repositories()
        return seen

    with caplog.at_level(logging.WARNING, logger=_RECEIVER_LOGGER):
        seen = asyncio.run(scenario())

    # Every failed read releases the receipt for another try, and it is never "failed".
    assert seen["receipt_1"] == Receipt(
        1, retry_after=True, deferred=False, failed=False, projected=False
    )
    assert seen["receipt_2"] == Receipt(
        2, retry_after=True, deferred=False, failed=False, projected=False
    )
    # The third attempt defers it: neither failed nor projected, nothing stored.
    assert seen["receipt_3"] == Receipt(
        3, retry_after=False, deferred=True, failed=False, projected=False
    )
    assert seen["handled_1"] == seen["handled_2"] == seen["handled_3"] == 1
    assert seen["rows_after_exhaustion"] == []
    assert github.requests[:3] == [("GET", REPOSITORY_PATH)] * 3
    assert any(
        "deferred after its last attempt" in record.getMessage()
        and "deferred_repository_details" in record.getMessage()
        for record in caplog.records
        if record.name == _RECEIVER_LOGGER
    )
    # Once deferred the receipt is not selected and GitHub is not asked again.
    assert seen["handled_extra"] == 0
    assert seen["receipt_extra"] == seen["receipt_3"]
    assert seen["extra_reads"] == 0
    # A GitHub login (wake_receipts) gives the receipt a fresh budget ...
    assert seen["receipt_woken"] == Receipt(
        0, retry_after=False, deferred=False, failed=False, projected=False
    )
    # ... and once GitHub answers, the repository is stored with the fetched fields.
    assert seen["handled_after_wake"] == 1
    assert seen["receipt_final"] == Receipt(
        0, retry_after=False, deferred=False, failed=False, projected=True
    )
    assert seen["rows_final"] == [("trunk", f"https://example.test/{FULL_NAME}")]


@pytest.mark.integration
def test_a_details_response_without_branch_or_url_fails_for_good_and_a_login_does_not_revive_it(
    database: Database,
) -> None:
    """A permanent fault stays on the failed-dispatch path: bounded, terminal, never woken."""
    github = GitHub(details_status=200, details_body={"id": 1000005, "full_name": FULL_NAME})

    async def scenario() -> dict[str, object]:
        seen: dict[str, object] = {}
        async with _worker(database, github) as worker:
            await worker.receive()
            for step in (1, 2, 3):
                seen[f"handled_{step}"] = await worker.sweep()
                seen[f"receipt_{step}"] = await worker.receipt()
            reads_after_exhaustion = github.repository_reads()

            await worker.wake()
            seen["receipt_woken"] = await worker.receipt()
            seen["handled_after_wake"] = await worker.sweep()
            seen["extra_reads"] = github.repository_reads() - reads_after_exhaustion
            seen["rows"] = await worker.repositories()
        return seen

    seen = asyncio.run(scenario())

    assert seen["receipt_1"] == Receipt(
        1, retry_after=True, deferred=False, failed=False, projected=False
    )
    assert seen["receipt_2"] == Receipt(
        2, retry_after=True, deferred=False, failed=False, projected=False
    )
    # After the third failure the receipt is failed, not deferred.
    assert seen["receipt_3"] == Receipt(
        3, retry_after=False, deferred=False, failed=True, projected=False
    )
    # A failing dispatch is not a handled one.
    assert seen["handled_1"] == seen["handled_2"] == seen["handled_3"] == 0
    # The wake resets the counter but keeps the failure mark, so nothing is selected again.
    assert seen["receipt_woken"] == Receipt(
        0, retry_after=False, deferred=False, failed=True, projected=False
    )
    assert seen["handled_after_wake"] == 0
    assert seen["extra_reads"] == 0
    assert seen["rows"] == []


@pytest.mark.integration
def test_unreadable_details_are_revived_by_the_hourly_tick_without_a_login(
    database: Database,
) -> None:
    """A GitHub outage that outlasts three attempts heals through the hourly revival alone."""
    github = GitHub()

    async def scenario() -> dict[str, object]:
        seen: dict[str, object] = {}
        async with _worker(database, github) as worker:
            await worker.receive()
            await worker.received_at(START)
            for _ in (1, 2, 3):
                await worker.sweep()
            # The third sweep deferred the receipt at 12:12; the clock now reads 12:18.
            seen["receipt_deferred"] = await worker.receipt()
            seen["handled_extra"] = await worker.sweep()
            seen["revived_at_12_24"] = await worker.revive()
            seen["rows_after_exhaustion"] = await worker.repositories()

            worker.clock[0] = datetime(2026, 10, 5, 12, 58, tzinfo=UTC)
            seen["revived_at_12_58"] = await worker.revive()
            seen["receipt_revived"] = await worker.receipt()

            github.answer_with_details()
            seen["handled_after_revival"] = await worker.sweep()
            seen["receipt_final"] = await worker.receipt()
            seen["rows_final"] = await worker.repositories()
        return seen

    seen = asyncio.run(scenario())

    assert seen["receipt_deferred"] == Receipt(
        3, retry_after=False, deferred=True, failed=False, projected=False
    )
    assert seen["handled_extra"] == 0
    # Twelve minutes after the deferral the hourly tick leaves it alone ...
    assert seen["revived_at_12_24"] == 0
    assert seen["rows_after_exhaustion"] == []
    # ... forty-six minutes after it, with no login, it gets a fresh attempt budget ...
    assert seen["revived_at_12_58"] == 1
    assert seen["receipt_revived"] == Receipt(
        0, retry_after=False, deferred=False, failed=False, projected=False
    )
    # ... and once GitHub answers again, the repository is stored with the fetched fields.
    assert seen["handled_after_revival"] == 1
    assert seen["receipt_final"] == Receipt(
        0, retry_after=False, deferred=False, failed=False, projected=True
    )
    assert seen["rows_final"] == [("trunk", f"https://example.test/{FULL_NAME}")]
    assert github.repository_reads() == 4


@pytest.mark.integration
def test_a_receipt_that_keeps_failing_is_revived_again_at_the_next_hourly_tick(
    database: Database,
) -> None:
    """A revived cycle is spent within the hour, so every hourly tick revives the receipt."""
    github = GitHub()

    async def scenario() -> dict[str, object]:
        seen: dict[str, object] = {}
        async with _worker(database, github) as worker:
            await worker.receive()
            await worker.received_at(START)
            for _ in (1, 2, 3):
                await worker.sweep()
            # Deferred at 12:12.
            worker.clock[0] = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)
            seen["revived_at_13_00"] = await worker.revive()
            for step in (1, 2, 3):
                seen[f"handled_{step}"] = await worker.sweep()
            # GitHub still answers 404: deferred again at 13:12; the clock now reads 13:18.
            seen["receipt_after_second_cycle"] = await worker.receipt()

            worker.clock[0] = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
            seen["revived_at_14_00"] = await worker.revive()
            seen["receipt_revived_again"] = await worker.receipt()
        return seen

    seen = asyncio.run(scenario())

    assert seen["revived_at_13_00"] == 1
    assert seen["handled_1"] == seen["handled_2"] == seen["handled_3"] == 1
    assert seen["receipt_after_second_cycle"] == Receipt(
        3, retry_after=False, deferred=True, failed=False, projected=False
    )
    # The tick an hour after the previous one finds it deferred 48 minutes ago: revived again.
    assert seen["revived_at_14_00"] == 1
    assert seen["receipt_revived_again"] == Receipt(
        0, retry_after=False, deferred=False, failed=False, projected=False
    )
    assert github.repository_reads() == 6


@pytest.mark.integration
def test_a_receipt_received_eight_days_before_the_revival_tick_stays_deferred(
    database: Database,
) -> None:
    """Outside the seven-day window only a login or a new delivery revives the receipt."""
    github = GitHub()

    async def scenario() -> dict[str, object]:
        seen: dict[str, object] = {}
        async with _worker(database, github) as worker:
            await worker.receive()
            await worker.received_at(START)
            for _ in (1, 2, 3):
                await worker.sweep()

            github.answer_with_details()
            worker.clock[0] = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
            seen["revived"] = await worker.revive()
            seen["receipt"] = await worker.receipt()
            seen["handled"] = await worker.sweep()
            seen["rows"] = await worker.repositories()
        return seen

    seen = asyncio.run(scenario())

    assert seen["revived"] == 0
    assert seen["receipt"] == Receipt(
        3, retry_after=False, deferred=True, failed=False, projected=False
    )
    assert seen["handled"] == 0
    assert seen["rows"] == []
    assert github.repository_reads() == 3
