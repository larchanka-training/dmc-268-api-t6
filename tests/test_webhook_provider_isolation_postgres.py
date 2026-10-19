"""Provider-bound receipt operations cannot mutate another provider's durable rows."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
    SqlAlchemyWebhookReceiptOperations,
)
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
GITHUB_ORIGIN = UUID("00000000-0000-0000-0000-000000000001")
GITLAB_ORIGIN = UUID("00000000-0000-0000-0000-000000000002")
TOKEN = UUID("00000000-0000-0000-0000-000000000003")


@dataclass(frozen=True)
class Database:
    url: str
    schema: str

    @asynccontextmanager
    async def sessions(self) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
        engine = create_async_engine(
            self.url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_receipt_provider_{uuid4().hex}"
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for provider, origin in (("github", GITHUB_ORIGIN), ("gitlab", GITLAB_ORIGIN)):
                workspace = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'T6', 7)"
                    ),
                    {"id": workspace},
                )
                connection.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id, workspace_id, provider, external_id, metadata) "
                        "VALUES (:id, :workspace, :provider, 99, '{}')"
                    ),
                    {"id": origin, "workspace": workspace, "provider": provider},
                )
                if provider == "gitlab":
                    connection.execute(
                        text(
                            "INSERT INTO gitlab_connections "
                            "(provider_installation_id, workspace_id, instance_origin, "
                            "token_ciphertext, credential_key_id, webhook_secret_digest) "
                            "VALUES (:id, :workspace, 'https://gitlab.example', "
                            ":envelope, 'test', :digest)"
                        ),
                        {
                            "id": origin,
                            "workspace": workspace,
                            "envelope": b"synthetic-envelope",
                            "digest": "a" * 64,
                        },
                    )
            connection.commit()
            try:
                yield Database(url, schema)
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


async def seed_receipt(
    sessions: async_sessionmaker[AsyncSession],
    delivery_id: str,
    *,
    provider: str = "gitlab",
    received_at: datetime = NOW,
    receipt_id: UUID | None = None,
    event_name: str = "installation",
    **state: Any,
) -> None:
    async with sessions() as session:
        session.add(
            WebhookEvent(
                id=receipt_id or uuid4(),
                provider=provider,
                provider_installation_id=GITLAB_ORIGIN if provider == "gitlab" else None,
                gitlab_connection_id=GITLAB_ORIGIN if provider == "gitlab" else None,
                installation_external_id=99,
                delivery_id=delivery_id,
                event=event_name,
                payload={"installation": {"id": 99}, "action": "created"},
                received_at=received_at,
                **state,
            )
        )
        await session.commit()


async def snapshot(sessions: async_sessionmaker[AsyncSession], delivery_id: str) -> object:
    async with sessions() as session:
        return await session.scalar(
            text("SELECT row_to_json(e) FROM webhook_events e WHERE delivery_id = :id"),
            {"id": delivery_id},
        )


def test_github_save_refuses_gitlab_receipt_without_writing(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            delivery = WebhookReceipt(
                "spoofed-github-key",
                "installation",
                '{"secret":"payload-canary"}',
                provider="gitlab",
                origin_provider_installation_id=GITLAB_ORIGIN,
            )
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                with pytest.raises(
                    ValueError, match="^GitHub receipt store requires github provider$"
                ):
                    await uow.receipts.save(delivery)
                await uow.commit()
            assert await snapshot(sessions, "spoofed-github-key") is None

    asyncio.run(scenario())


def test_github_pending_selection_excludes_gitlab_and_preserves_fair_order(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await seed_receipt(sessions, "gitlab-first", received_at=NOW - timedelta(days=1))
            await seed_receipt(
                sessions,
                "github-second",
                provider="github",
                receipt_id=UUID("00000000-0000-0000-0000-000000000012"),
            )
            await seed_receipt(
                sessions,
                "github-first",
                provider="github",
                receipt_id=UUID("00000000-0000-0000-0000-000000000011"),
            )
            before = await snapshot(sessions, "gitlab-first")
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(NOW, 1) == ("github-first",)
                assert await uow.receipts.pending_ids(NOW, 5) == ("github-first", "github-second")
            assert await snapshot(sessions, "gitlab-first") == before

    asyncio.run(scenario())


def test_github_direct_claim_cannot_claim_gitlab_delivery_id(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await seed_receipt(sessions, "same-public-delivery-id")
            before = await snapshot(sessions, "same-public-delivery-id")
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.claim(
                        "same-public-delivery-id", TOKEN, NOW, NOW + timedelta(minutes=5)
                    )
                    is None
                )
                await uow.commit()
            assert await snapshot(sessions, "same-public-delivery-id") == before

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["project", "trigger_retry", "defer", "failure"])
def test_github_transitions_refuse_other_provider_even_with_exact_id_and_token(
    database: Database,
    operation: str,
) -> None:
    async def transition(uow: SqlAlchemyGitHubWebhookReceiptUnitOfWork, key: str) -> object:
        if operation == "project":
            await uow.receipts.mark_projected(key, TOKEN, NOW)
            return None
        if operation == "trigger_retry":
            return await uow.receipts.retry_run_trigger(
                key,
                TOKEN,
                NOW + timedelta(seconds=30),
                now=NOW,
                received_before=NOW - timedelta(hours=1),
            )
        if operation == "defer":
            return await uow.receipts.release(key, TOKEN, NOW + timedelta(minutes=5), NOW, 3)
        return await uow.receipts.release_after_dispatch_failure(
            key,
            TOKEN,
            NOW + timedelta(seconds=30),
            NOW,
            3,
        )

    async def scenario() -> None:
        async with database.sessions() as sessions:
            for provider in ("gitlab", "github"):
                await seed_receipt(
                    sessions,
                    provider,
                    provider=provider,
                    projection_claim_token=TOKEN,
                    projection_lease_until=NOW + timedelta(minutes=5),
                    projection_attempt_count=2,
                )
            before = await snapshot(sessions, "gitlab")
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                if operation in {"project", "trigger_retry"}:
                    with pytest.raises(RuntimeError, match="^webhook projection claim was lost$"):
                        await transition(uow, "gitlab")
                else:
                    assert await transition(uow, "gitlab") is False
                await uow.commit()
            assert await snapshot(sessions, "gitlab") == before
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                result = await transition(uow, "github")
                assert result is (None if operation == "project" else operation != "trigger_retry")
                await uow.commit()
            after = await snapshot(sessions, "github")
            assert isinstance(after, dict)
            assert after["projection_claim_token"] is None
            assert after["projection_lease_until"] is None
            if operation == "project":
                assert after["projected_at"] is not None
            elif operation == "trigger_retry":
                assert after["projection_attempt_count"] == 0
                assert after["retry_after"] is not None
            else:
                assert after["projection_attempt_count"] == 3
                assert after["retry_after"] is None
                field = "projection_deferred_at" if operation == "defer" else "projection_failed_at"
                assert after[field] is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "finished_field", ["projected_at", "projection_failed_at", "projection_deferred_at"]
)
def test_github_retention_keeps_finished_gitlab_receipts(
    database: Database,
    finished_field: str,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            old_state: dict[str, Any] = {finished_field: NOW - timedelta(days=31)}
            recent_state: dict[str, Any] = {finished_field: NOW}
            for provider in ("github", "gitlab"):
                await seed_receipt(
                    sessions,
                    provider,
                    provider=provider,
                    **old_state,
                )
            await seed_receipt(sessions, "recent", provider="github", **recent_state)
            before = await snapshot(sessions, "gitlab")
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.purge_finished(NOW - timedelta(days=30)) == 1
                await uow.commit()
            assert await snapshot(sessions, "gitlab") == before
            assert await snapshot(sessions, "github") is None
            assert await snapshot(sessions, "recent") is not None

    asyncio.run(scenario())


def test_github_revival_keeps_same_numeric_installation_gitlab_receipt_deferred(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            for provider in ("github", "gitlab"):
                await seed_receipt(
                    sessions,
                    provider,
                    provider=provider,
                    received_at=NOW - timedelta(days=1),
                    projection_deferred_at=NOW - timedelta(hours=1),
                    projection_attempt_count=3,
                )
            before = await snapshot(sessions, "gitlab")
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.revive_deferred_installation_deliveries(
                        deferred_before=NOW - timedelta(minutes=45),
                        received_after=NOW - timedelta(days=7),
                    )
                    == 1
                )
                await uow.commit()
            assert await snapshot(sessions, "gitlab") == before
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(NOW, 5) == ("github",)

    asyncio.run(scenario())


@pytest.mark.parametrize("event_name", ["installation", "pull_request"])
def test_github_link_wake_keeps_other_provider_with_same_numeric_installation_deferred(
    database: Database,
    event_name: str,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            for provider in ("github", "gitlab"):
                await seed_receipt(
                    sessions,
                    provider,
                    provider=provider,
                    event_name=event_name,
                    projection_deferred_at=NOW - timedelta(hours=1),
                    retry_after=NOW + timedelta(minutes=5),
                    projection_attempt_count=3,
                )
            before = await snapshot(sessions, "gitlab")
            async with SqlAlchemyGitHubInstallationLinkUnitOfWork(sessions) as uow:
                await uow.links.wake_receipts(99)
                await uow.commit()
            assert await snapshot(sessions, "gitlab") == before
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(NOW, 5) == ("github",)

    asyncio.run(scenario())


def test_github_save_sets_provider_explicitly_and_claim_returns_persisted_origin(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            async with sessions() as session:
                await session.execute(
                    text("ALTER TABLE webhook_events ALTER COLUMN provider SET DEFAULT 'gitlab'")
                )
                await session.commit()
            receipt = WebhookReceipt(
                "explicit-github",
                "ping",
                "{}",
                origin_provider_installation_id=GITHUB_ORIGIN,
            )
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.save(receipt) is True
                await uow.commit()
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                claimed = await uow.receipts.claim(
                    "explicit-github",
                    TOKEN,
                    NOW,
                    NOW + timedelta(minutes=5),
                )
                assert claimed == receipt
                await uow.commit()
            row = await snapshot(sessions, "explicit-github")
            assert isinstance(row, dict)
            assert row["provider"] == "github"
            assert row["provider_installation_id"] == str(GITHUB_ORIGIN)
            assert row["gitlab_connection_id"] is None

    asyncio.run(scenario())


def test_github_spoofed_duplicate_key_cannot_replace_gitlab_receipt(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await seed_receipt(sessions, "spoofed-shared-key")
            before = await snapshot(sessions, "spoofed-shared-key")
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.save(
                        WebhookReceipt("spoofed-shared-key", "ping", '{"different":"payload"}')
                    )
                    is False
                )
                await uow.commit()
            assert await snapshot(sessions, "spoofed-shared-key") == before

    asyncio.run(scenario())


def test_shared_gitlab_mechanics_keep_origin_reclaim_lease_and_preserve_github_markers(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await seed_receipt(sessions, "gitlab")
            await seed_receipt(
                sessions, "github", provider="github", projected_at=NOW - timedelta(days=31)
            )
            github_before = await snapshot(sessions, "github")
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO github_installation_removal_effects (delivery_id, created_at) "
                        "VALUES ('github', :old), ('orphan-marker', :old)"
                    ),
                    {"old": NOW - timedelta(days=31)},
                )
                await session.commit()
            async with sessions() as session:
                operations = SqlAlchemyWebhookReceiptOperations(session, provider="gitlab")
                assert await operations.pending_ids(NOW, 5) == ("gitlab",)
                claimed = await operations.claim("gitlab", TOKEN, NOW, NOW + timedelta(minutes=5))
                assert claimed is not None
                assert claimed.provider == "gitlab"
                assert claimed.origin_provider_installation_id == GITLAB_ORIGIN
                await session.commit()
            replacement = uuid4()
            async with sessions() as session:
                operations = SqlAlchemyWebhookReceiptOperations(session, provider="gitlab")
                assert (
                    await operations.claim("gitlab", replacement, NOW, NOW + timedelta(minutes=5))
                    is None
                )
                assert (
                    await operations.claim(
                        "gitlab",
                        replacement,
                        NOW + timedelta(minutes=5),
                        NOW + timedelta(minutes=10),
                    )
                    == claimed
                )
                await session.commit()
            async with sessions() as session:
                operations = SqlAlchemyWebhookReceiptOperations(session, provider="gitlab")
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await operations.mark_projected("gitlab", TOKEN, NOW)
                await operations.mark_projected("gitlab", replacement, NOW)
                await session.commit()
            async with sessions() as session:
                operations = SqlAlchemyWebhookReceiptOperations(session, provider="gitlab")
                assert await operations.purge_finished(NOW + timedelta(days=31)) == 1
                await session.commit()
            assert await snapshot(sessions, "gitlab") is None
            assert await snapshot(sessions, "github") == github_before
            async with sessions() as session:
                assert tuple(
                    (
                        await session.scalars(
                            text(
                                "SELECT delivery_id FROM github_installation_removal_effects "
                                "ORDER BY delivery_id"
                            )
                        )
                    ).all()
                ) == ("github", "orphan-marker")

    asyncio.run(scenario())
