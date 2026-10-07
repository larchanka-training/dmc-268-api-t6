"""Deferred GitHub deliveries stop after three attempts and wake on linking (#52).

Opt-in with ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    WebhookReceipt,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)

START = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Database:
    url: str
    schema: str


@pytest.fixture
def database() -> Iterator[Database]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_deferred_{uuid4().hex}"
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


class UnknownInstallation:
    """The dispatcher answer for a delivery of an installation nobody linked yet."""

    def __init__(self) -> None:
        self.calls = 0
        self.known = False

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        self.calls += 1
        return InstallationDeliveryDispatchResult(
            InstallationDeliveryDispatchStatus.ONBOARDED
            if self.known
            else InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION
        )


def _delivery() -> WebhookReceipt:
    return VerifiedGitHubDelivery(
        "delivery-1",
        "installation",
        {"action": "created", "installation": {"id": 99}, "sender": {"type": "User"}},
    ).to_receipt()


@pytest.mark.integration
def test_deferred_delivery_is_final_after_three_attempts_and_wakes_on_linking(
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
        clock = [START]
        dispatcher = UnknownInstallation()
        receiver = ReceiveGitHubDelivery(
            uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory),
            dispatcher=dispatcher,
            now=lambda: clock[0],
        )

        async def row() -> tuple[Any, ...]:
            async with factory() as session:
                return tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT projection_attempt_count, "
                                "projection_deferred_at IS NOT NULL, retry_after IS NULL "
                                "FROM webhook_events"
                            )
                        )
                    ).one()
                )

        await receiver.execute(_delivery())
        for _ in range(5):
            await receiver.replay_pending()
            clock[0] += timedelta(minutes=6)
        final = await row()
        calls_before_wake = dispatcher.calls

        async with SqlAlchemyGitHubInstallationLinkUnitOfWork(factory) as uow:
            await uow.links.wake_receipts(99)
            await uow.commit()
        woken = await row()
        dispatcher.known = True
        handled = await receiver.replay_pending()
        async with factory() as session:
            projected = await session.scalar(
                text("SELECT projected_at IS NOT NULL FROM webhook_events")
            )

        clock[0] += timedelta(days=31)
        purged = await receiver.purge_finished()
        async with factory() as session:
            left = await session.scalar(text("SELECT count(*) FROM webhook_events"))
        await engine.dispose()
        return {
            "final": final,
            "calls": calls_before_wake,
            "woken": woken,
            "handled": handled,
            "projected": projected,
            "purged": purged,
            "left": left,
        }

    result = asyncio.run(scenario())

    # Three attempts, then a final deferral: no more retries, no more dispatches.
    assert result["final"] == (3, True, True)
    assert result["calls"] == 3
    # Linking the installation wakes the delivery with a fresh attempt budget.
    assert result["woken"] == (0, False, True)
    assert result["handled"] == 1 and result["projected"] is True
    # Finished receipts are purged after the 30-day retention.
    assert (result["purged"], result["left"]) == (1, 0)


def _receiver(
    database: Database, dispatcher: UnknownInstallation, clock: list[datetime]
) -> tuple[ReceiveGitHubDelivery, async_sessionmaker[AsyncSession], Any]:
    engine = create_async_engine(
        database.url,
        connect_args={"options": f"-csearch_path={database.schema}"},
        poolclass=NullPool,
    )
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory),
        dispatcher=dispatcher,
        now=lambda: clock[0],
    )
    return receiver, factory, engine


@pytest.mark.integration
def test_sweep_summary_counts_deliveries_deferred_for_good(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        clock = [START]
        receiver, _, engine = _receiver(database, UnknownInstallation(), clock)
        await receiver.execute(_delivery())
        for _ in range(3):
            await receiver.replay_pending()
            clock[0] += timedelta(minutes=6)
        await engine.dispose()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())

    sweeps = [
        (r.levelno, r.getMessage()) for r in caplog.records if "webhook sweep" in r.getMessage()
    ]
    assert sweeps == [
        (logging.INFO, "GitHub webhook sweep: 1 handled, 1 deferred, 0 deferred for good"),
        (logging.INFO, "GitHub webhook sweep: 1 handled, 1 deferred, 0 deferred for good"),
        (logging.INFO, "GitHub webhook sweep: 1 handled, 1 deferred, 1 deferred for good"),
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "GitHub webhook delivery delivery-1 deferred after its last attempt: "
        "ignored_unknown_installation"
    ]


@pytest.mark.integration
def test_purge_counts_projected_failed_and_deferred_receipts_past_retention(
    database: Database,
) -> None:
    async def scenario() -> tuple[int, list[str]]:
        clock = [START]
        receiver, factory, engine = _receiver(database, UnknownInstallation(), clock)
        old = START - timedelta(days=31)
        fresh = START - timedelta(days=29)
        async with factory() as session:
            for delivery_id, projected, failed, deferred in (
                ("projected-old", old, None, None),
                ("failed-old", None, old, None),
                ("deferred-old", None, None, old),
                ("projected-fresh", fresh, None, None),
            ):
                await session.execute(
                    text(
                        "INSERT INTO webhook_events (id, delivery_id, event, payload, "
                        "projected_at, projection_failed_at, projection_deferred_at) "
                        "VALUES (gen_random_uuid(), :delivery_id, 'installation', "
                        "CAST('{}' AS jsonb), :projected, :failed, :deferred)"
                    ),
                    {
                        "delivery_id": delivery_id,
                        "projected": projected,
                        "failed": failed,
                        "deferred": deferred,
                    },
                )
            await session.commit()
        purged = await receiver.purge_finished()
        async with factory() as session:
            left = list(await session.scalars(text("SELECT delivery_id FROM webhook_events")))
        await engine.dispose()
        return purged, left

    purged, left = asyncio.run(scenario())

    # The 30-day retention removes each kind of finished receipt and counts all of them.
    assert (purged, left) == (3, ["projected-fresh"])


@pytest.mark.integration
@pytest.mark.parametrize(
    ("event", "payload"),
    [
        (
            "pull_request",
            {
                "action": "labeled",
                "number": 7,
                "installation": {"id": 99},
                "repository": {"id": 101},
                "sender": {"type": "User"},
            },
        ),
        (
            "check_suite",
            {
                "action": "completed",
                "installation": {"id": 99},
                "repository": {"id": 101},
                "check_suite": {"head_sha": "e" * 40},
            },
        ),
        (
            "status",
            {
                "installation": {"id": 99},
                "repository": {"id": 101},
                "sha": "e" * 40,
                "state": "success",
            },
        ),
    ],
)
def test_linking_wakes_a_pr_or_ci_delivery_deferred_for_good(
    database: Database, event: str, payload: dict[str, Any]
) -> None:
    async def scenario() -> tuple[tuple[Any, ...], tuple[Any, ...], int]:
        clock = [START]
        dispatcher = UnknownInstallation()
        receiver, factory, engine = _receiver(database, dispatcher, clock)
        await receiver.execute(VerifiedGitHubDelivery("delivery-1", event, payload).to_receipt())
        for _ in range(3):
            await receiver.replay_pending()
            clock[0] += timedelta(minutes=6)

        async def row() -> tuple[Any, ...]:
            async with factory() as session:
                result = await session.execute(
                    text(
                        "SELECT projection_attempt_count, projection_deferred_at IS NOT NULL "
                        "FROM webhook_events"
                    )
                )
                return tuple(result.one())

        final = await row()
        async with SqlAlchemyGitHubInstallationLinkUnitOfWork(factory) as uow:
            await uow.links.wake_receipts(99)
            await uow.commit()
        woken = await row()
        dispatcher.known = True
        handled = await receiver.replay_pending()
        await engine.dispose()
        return final, woken, handled

    final, woken, handled = asyncio.run(scenario())

    assert final == (3, True)
    # Only the projection_deferred_at branch of wake_receipts covers non-installation events.
    assert woken == (0, False)
    # The sweep selects the woken delivery again.
    assert handled == 1


@pytest.mark.integration
@pytest.mark.parametrize("commit", [False, True])
def test_removal_markers_follow_receipt_retention_atomically(
    database: Database, commit: bool
) -> None:
    async def scenario() -> None:
        receiver, factory, engine = _receiver(database, UnknownInstallation(), [START])
        cutoff = START - timedelta(days=30)
        old = cutoff - timedelta(seconds=1)
        marker_ids = [
            "boundary",
            "fresh",
            "leased",
            "old-finished",
            "orphan",
            "pending",
            "recent-finished",
        ]
        try:
            async with factory() as session:
                for delivery_id in marker_ids:
                    await session.execute(
                        text(
                            "INSERT INTO github_installation_removal_effects "
                            "(delivery_id, created_at) VALUES (:id, :created_at)"
                        ),
                        {
                            "id": delivery_id,
                            "created_at": cutoff
                            if delivery_id == "boundary"
                            else START
                            if delivery_id == "fresh"
                            else old,
                        },
                    )
                for delivery_id, projected, lease in (
                    ("old-finished", old, None),
                    ("recent-finished", START, None),
                    ("pending", None, None),
                    ("leased", None, START + timedelta(minutes=5)),
                ):
                    await session.execute(
                        text(
                            "INSERT INTO webhook_events (id, delivery_id, event, payload, "
                            "projected_at, projection_claim_token, projection_lease_until) "
                            "VALUES (gen_random_uuid(), :id, 'installation', '{}'::jsonb, "
                            ":projected, :token, :lease)"
                        ),
                        {
                            "id": delivery_id,
                            "projected": projected,
                            "token": uuid4() if lease else None,
                            "lease": lease,
                        },
                    )
                await session.commit()
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as uow:
                assert await uow.receipts.purge_finished(cutoff) == 1
                if commit:
                    await uow.commit()
            async with factory() as session:
                markers = list(
                    await session.scalars(
                        text(
                            "SELECT delivery_id FROM github_installation_removal_effects "
                            "ORDER BY delivery_id"
                        )
                    )
                )
                receipts = list(
                    await session.scalars(
                        text("SELECT delivery_id FROM webhook_events ORDER BY delivery_id")
                    )
                )
            assert markers == (
                ["boundary", "fresh", "leased", "pending", "recent-finished"]
                if commit
                else marker_ids
            )
            assert receipts == (
                ["leased", "pending", "recent-finished"]
                if commit
                else ["leased", "old-finished", "pending", "recent-finished"]
            )
            if commit:
                # Marker-only cleanup must run even when no receipt is eligible.
                async with factory() as session:
                    await session.execute(
                        text(
                            "INSERT INTO github_installation_removal_effects "
                            "(delivery_id, created_at) "
                            "VALUES ('second-orphan', :old)"
                        ),
                        {"old": old},
                    )
                    await session.commit()
                assert await receiver.purge_finished() == 0
                async with factory() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM github_installation_removal_effects "
                                "WHERE delivery_id = 'second-orphan'"
                            )
                        )
                        == 0
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
