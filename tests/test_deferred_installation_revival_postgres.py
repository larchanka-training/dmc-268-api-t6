"""Hourly revival of deferred installation deliveries of linked installations (api#71).

The real receipt store and ``ReviveDeferredInstallationDeliveries`` run against a migrated
schema; receipts are seeded directly. Opt-in with ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.workspaces.infrastructure.models import Workspace

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
LINKED = 1000001
UNLINKED = 1000002
GITLAB_ONLY = 1000003
# Deferred two hours before NOW and received a day before it: otherwise eligible.
DEFERRED = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
RECEIVED = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Database:
    url: str
    schema: str


@pytest.fixture
def database() -> Iterator[Database]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_revival_{uuid4().hex}"
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


def _receipt(
    delivery_id: str,
    *,
    event: str = "installation_repositories",
    installation: int = LINKED,
    received_at: datetime = RECEIVED,
    deferred_at: datetime = DEFERRED,
    projected_at: datetime | None = None,
    failed_at: datetime | None = None,
) -> WebhookEvent:
    """A receipt after its third and last attempt, deferred at ``deferred_at``."""
    return WebhookEvent(
        delivery_id=delivery_id,
        event=event,
        installation_external_id=installation,
        payload={"installation": {"id": installation}},
        received_at=received_at,
        projection_deferred_at=deferred_at,
        projection_attempt_count=3,
        projected_at=projected_at,
        projection_failed_at=failed_at,
    )


def _seed() -> list[WebhookEvent]:
    return [
        _receipt(
            "revived",
            deferred_at=datetime(2026, 10, 6, 11, 14, tzinfo=UTC),
            received_at=datetime(2026, 9, 29, 13, 0, tzinfo=UTC),
        ),
        _receipt(
            "installation-event",
            event="installation",
            deferred_at=datetime(2026, 10, 6, 10, 59, tzinfo=UTC),
        ),
        _receipt(
            "deferred-exactly-45-minutes-ago",
            deferred_at=datetime(2026, 10, 6, 11, 15, tzinfo=UTC),
            received_at=datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
        ),
        _receipt("deferred-44-minutes-ago", deferred_at=datetime(2026, 10, 6, 11, 16, tzinfo=UTC)),
        _receipt(
            "received-7-days-1-hour-ago", received_at=datetime(2026, 9, 29, 11, 0, tzinfo=UTC)
        ),
        _receipt("failed", failed_at=datetime(2026, 10, 6, 9, 0, tzinfo=UTC)),
        _receipt("projected", projected_at=datetime(2026, 10, 6, 9, 0, tzinfo=UTC)),
        _receipt("pull-request-event", event="pull_request"),
        _receipt("check-suite-event", event="check_suite"),
        _receipt("unlinked-installation", installation=UNLINKED),
        _receipt("gitlab-installation", installation=GITLAB_ONLY),
        _receipt("payload-in-s3"),
    ]


@pytest.mark.integration
def test_revival_resets_only_old_enough_recent_installation_events_of_linked_installations(
    database: Database,
) -> None:
    async def scenario() -> dict[str, Any]:
        engine = create_async_engine(
            database.url,
            connect_args={"options": f"-csearch_path={database.schema}"},
            poolclass=NullPool,
        )
        factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )
        try:
            async with factory() as session:
                workspace = Workspace(id=uuid4(), name="revival", daily_budget_usd=Decimal("1"))
                session.add(workspace)
                await session.flush()
                for provider, external_id in (("github", LINKED), ("gitlab", GITLAB_ONLY)):
                    session.add(
                        ProviderInstallation(
                            id=uuid4(),
                            workspace_id=workspace.id,
                            provider=provider,
                            external_id=external_id,
                            provider_metadata={},
                        )
                    )
                await session.flush()
                session.add_all(_seed())
                await session.flush()
                # SQL NULL (a Python None would be stored as JSON null): the body lives in S3.
                await session.execute(
                    text(
                        "UPDATE webhook_events SET payload = NULL, "
                        "payload_s3_ref = 's3://receipts/payload-in-s3.json' "
                        "WHERE delivery_id = 'payload-in-s3'"
                    )
                )
                await session.commit()

            reviver = ReviveDeferredInstallationDeliveries(
                uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory),
                now=lambda: NOW,
            )
            first = await reviver.execute()
            second = await reviver.execute()

            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as uow:
                pending = await uow.receipts.pending_ids(NOW, 100)
            async with factory() as session:
                rows = (
                    await session.execute(
                        select(
                            WebhookEvent.delivery_id,
                            WebhookEvent.projection_deferred_at,
                            WebhookEvent.retry_after,
                            WebhookEvent.projection_attempt_count,
                        )
                    )
                ).all()
            return {
                "first": first,
                "second": second,
                "pending": pending,
                "rows": {row[0]: tuple(row[1:]) for row in rows},
            }
        finally:
            await engine.dispose()

    result = asyncio.run(scenario())

    # The installation events of the linked installation deferred at least 45 minutes ago
    # (exactly 45 included) and received within the last seven days are revived; nothing is
    # left a second later.
    assert result["first"] == 3
    assert result["second"] == 0
    assert result["rows"]["revived"] == (None, None, 0)
    assert result["rows"]["deferred-exactly-45-minutes-ago"] == (None, None, 0)
    assert result["rows"]["installation-event"] == (None, None, 0)
    assert result["pending"] == (
        "revived",
        "deferred-exactly-45-minutes-ago",
        "installation-event",
    )
    # Everything else keeps its deferral mark and spent attempts.
    deferred_two_hours_ago = (datetime(2026, 10, 6, 10, 0, tzinfo=UTC), None, 3)
    assert result["rows"]["deferred-44-minutes-ago"] == (
        datetime(2026, 10, 6, 11, 16, tzinfo=UTC),
        None,
        3,
    )
    assert result["rows"]["received-7-days-1-hour-ago"] == deferred_two_hours_ago
    assert result["rows"]["failed"] == deferred_two_hours_ago
    assert result["rows"]["projected"] == deferred_two_hours_ago
    assert result["rows"]["pull-request-event"] == deferred_two_hours_ago
    assert result["rows"]["check-suite-event"] == deferred_two_hours_ago
    assert result["rows"]["payload-in-s3"] == deferred_two_hours_ago
    assert result["rows"]["unlinked-installation"] == deferred_two_hours_ago
    assert result["rows"]["gitlab-installation"] == deferred_two_hours_ago
