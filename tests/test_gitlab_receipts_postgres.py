"""GitLab receipt admission, immutable origin and leases against real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.gitlab.application.webhook_receipts import (
    GitLabReceiptError,
    GitLabReceiptOriginInactive,
    prepare_gitlab_receipt,
)
from app.modules.integrations.gitlab.infrastructure.webhook_receipts import (
    SqlAlchemyGitLabWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveWebhookDelivery,
    WebhookReceipt,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
ORIGIN = UUID("00000000-0000-0000-0000-000000000002")
OTHER = UUID("00000000-0000-0000-0000-000000000004")
REPLACEMENT = UUID("00000000-0000-0000-0000-000000000005")
TOKEN = UUID("00000000-0000-0000-0000-000000000003")
PAYLOAD = (
    '{"object_kind":"merge_request","project":{"id":17},"object_attributes":{"action":"open"}}'
)


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
    schema = f"test_gitlab_receipts_{uuid4().hex}"
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for number, origin in enumerate((ORIGIN, OTHER), 99):
                workspace = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'T6b', 7)"
                    ),
                    {"id": workspace},
                )
                connection.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id, workspace_id, provider, external_id, metadata) "
                        "VALUES (:id, :workspace, 'gitlab', :number, '{}')"
                    ),
                    {"id": origin, "workspace": workspace, "number": number},
                )
                instance = f"https://gitlab{number}.example"
                connection.execute(
                    text(
                        "INSERT INTO gitlab_connections "
                        "(provider_installation_id, workspace_id, instance_origin, "
                        "token_ciphertext, credential_key_id, webhook_secret_digest, "
                        "intake_enabled) "
                        "VALUES (:id, :workspace, :instance, :envelope, 'test', :digest, true)"
                    ),
                    {
                        "id": origin,
                        "workspace": workspace,
                        "instance": instance,
                        "envelope": b"synthetic-envelope",
                        "digest": "a" * 64,
                    },
                )
                repository = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO repositories (id, provider_installation_id, external_id, "
                        "full_name, default_branch, web_url) "
                        "VALUES (:id, :origin, 17, 'team/repo', 'main', :url)"
                    ),
                    {"id": repository, "origin": origin, "url": instance + "/team/repo"},
                )
                connection.execute(
                    text(
                        "INSERT INTO gitlab_project_bindings (repository_id, "
                        "provider_installation_id, instance_origin, project_external_id) "
                        "VALUES (:id, :origin, :instance, 17)"
                    ),
                    {"id": repository, "origin": origin, "instance": instance},
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


def receipt(
    *,
    origin: UUID = ORIGIN,
    key: str = "abc",
    payload: str = PAYLOAD,
    event: str = "Merge Request Hook",
) -> WebhookReceipt:
    return prepare_gitlab_receipt(
        connection_id=origin, webhook_id=key, event_name=event, payload_json=payload
    )


def receiver(
    sessions: async_sessionmaker[AsyncSession], dispatcher: RecordingDispatcher | None = None
) -> ReceiveWebhookDelivery:
    return ReceiveWebhookDelivery(
        provider="gitlab",
        uow_factory=lambda: SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions),
        dispatcher=dispatcher,
        now=lambda: NOW,
    )


class RecordingDispatcher:
    def __init__(
        self,
        status: InstallationDeliveryDispatchStatus | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.deliveries: list[WebhookReceipt] = []
        self.status = status or InstallationDeliveryDispatchStatus.PROJECTED_PR
        self.fail = fail

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        self.deliveries.append(delivery)
        if self.fail:
            raise RuntimeError("synthetic dispatch failure")
        return InstallationDeliveryDispatchResult(self.status)


async def rows(sessions: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    text("SELECT row_to_json(e) FROM webhook_events e ORDER BY delivery_id")
                )
            ).all()
        )


async def change_origin(sessions: async_sessionmaker[AsyncSession], change: str) -> None:
    statements = {
        "revoked": (
            "UPDATE gitlab_connections SET revoked_at = now(), intake_enabled = false "
            "WHERE provider_installation_id = :id"
        ),
        "disabled": (
            "UPDATE gitlab_connections SET intake_enabled = false "
            "WHERE provider_installation_id = :id"
        ),
        "unbound": "DELETE FROM gitlab_project_bindings WHERE provider_installation_id = :id",
        "unavailable": (
            "UPDATE repositories SET installation_available = false "
            "WHERE provider_installation_id = :id"
        ),
        "retired": "UPDATE provider_installations SET deleted_at = now() WHERE id = :id",
    }
    async with sessions() as session:
        if change == "rebound":
            await session.execute(
                text("DELETE FROM gitlab_project_bindings WHERE provider_installation_id = :id"),
                {"id": ORIGIN},
            )
            await session.execute(
                text(
                    "UPDATE repositories SET installation_available = false "
                    "WHERE provider_installation_id = :id"
                ),
                {"id": ORIGIN},
            )
            await session.execute(
                text(
                    "INSERT INTO provider_installations (id, workspace_id, provider, "
                    "external_id, metadata) SELECT :new, workspace_id, 'gitlab', 101, '{}' "
                    "FROM provider_installations WHERE id = :other"
                ),
                {"new": REPLACEMENT, "other": OTHER},
            )
            await session.execute(
                text(
                    "INSERT INTO gitlab_connections (provider_installation_id, workspace_id, "
                    "instance_origin, token_ciphertext, credential_key_id, webhook_secret_digest, "
                    "intake_enabled) SELECT :new, workspace_id, 'https://gitlab99.example', "
                    "token_ciphertext, credential_key_id, webhook_secret_digest, true "
                    "FROM gitlab_connections WHERE provider_installation_id = :other"
                ),
                {"new": REPLACEMENT, "other": OTHER},
            )
            repository_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO repositories (id, provider_installation_id, external_id, "
                    "full_name, default_branch, web_url) "
                    "VALUES (:id, :new, 17, 'replacement/repo', "
                    "'main', 'https://gitlab99.example/replacement/repo')"
                ),
                {"id": repository_id, "new": REPLACEMENT},
            )
            await session.execute(
                text(
                    "INSERT INTO gitlab_project_bindings (repository_id, "
                    "provider_installation_id, instance_origin, project_external_id) "
                    "VALUES (:id, :new, 'https://gitlab99.example', 17)"
                ),
                {"id": repository_id, "new": REPLACEMENT},
            )
        else:
            await session.execute(text(statements[change]), {"id": ORIGIN})
        await session.commit()


def test_concurrent_duplicate_admission_creates_one_receipt_with_exact_origin(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            delivery = receipt()
            outcomes = await asyncio.gather(
                *(receiver(sessions).execute(delivery) for _ in range(2))
            )
            assert sorted(item.value for item in outcomes) == ["duplicate", "pending"]
            persisted = await rows(sessions)
            assert len(persisted) == 1
            assert persisted[0]["provider"] == "gitlab"
            assert persisted[0]["provider_installation_id"] == str(ORIGIN)
            assert persisted[0]["gitlab_connection_id"] == str(ORIGIN)
            assert persisted[0]["installation_external_id"] == 99
            assert persisted[0]["delivery_id"] == delivery.delivery_id
            assert persisted[0]["action"] == "open"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change", ["revoked", "unbound", "unavailable", "disabled", "retired", "rebound"]
)
def test_replay_commits_inactive_origin_as_ignored_without_dispatch(
    database: Database,
    change: str,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await receiver(sessions).execute(receipt())
            await change_origin(sessions, change)
            dispatcher = RecordingDispatcher()
            engine = receiver(sessions, dispatcher)
            # A claim that terminalizes an inactive origin drains it but is not dispatched.
            assert await engine.replay_pending() == 0
            assert dispatcher.deliveries == []
            persisted = await rows(sessions)
            assert persisted[0]["projected_at"] is not None
            assert persisted[0]["projection_claim_token"] is None
            assert persisted[0]["projection_attempt_count"] == 0
            assert await engine.replay_pending() == 0
            assert await engine.purge_finished(timedelta(days=-31)) == 1
            assert await rows(sessions) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["payload", "event"])
def test_same_key_with_changed_content_conflicts_without_replacing_receipt(
    database: Database,
    changed: str,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await receiver(sessions).execute(receipt())
            before = await rows(sessions)
            different = (
                receipt(payload=PAYLOAD.replace('"open"', '"close"'))
                if changed == "payload"
                else receipt(event="Push Hook")
            )
            with pytest.raises(GitLabReceiptError, match="^delivery_conflict$"):
                await receiver(sessions).execute(different)
            assert await rows(sessions) == before

    asyncio.run(scenario())


def test_canonical_json_duplicate_and_two_instances_same_numeric_project_are_isolated(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await receiver(sessions).execute(receipt())
            reordered = (
                '{"project":{"id":17},"object_attributes":{"action":"open"},'
                '"object_kind":"merge_request"}'
            )
            assert (
                await receiver(sessions).execute(receipt(payload=reordered))
                == InstallationDeliveryDispatchStatus.DUPLICATE
            )
            assert (
                await receiver(sessions).execute(receipt(origin=OTHER))
                == InstallationDeliveryDispatchStatus.PENDING
            )
            dispatcher = RecordingDispatcher()
            assert await receiver(sessions, dispatcher).replay_pending() == 2
            assert {item.origin_provider_installation_id for item in dispatcher.deliveries} == {
                ORIGIN,
                OTHER,
            }
            assert {item.delivery_id for item in dispatcher.deliveries} == {
                receipt().delivery_id,
                receipt(origin=OTHER).delivery_id,
            }
            persisted = await rows(sessions)
            assert {row["installation_external_id"] for row in persisted} == {99, 100}
            assert all(row["projected_at"] is not None for row in persisted)

    asyncio.run(scenario())


def test_unsupported_non_project_event_is_durable_ignored_and_deduplicated(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            delivery = receipt(event="System Hook", payload='{"event_name":"user_create"}')
            assert (
                await receiver(sessions).execute(delivery)
                == InstallationDeliveryDispatchStatus.PENDING
            )
            assert (
                await receiver(sessions).execute(delivery)
                == InstallationDeliveryDispatchStatus.DUPLICATE
            )
            dispatcher = RecordingDispatcher()
            assert await receiver(sessions, dispatcher).replay_pending() == 0
            assert dispatcher.deliveries == []
            persisted = await rows(sessions)
            assert persisted[0]["payload"] == {"event_name": "user_create"}
            assert persisted[0]["projected_at"] is not None
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == 2
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 2
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change", ["revoked", "disabled", "unbound", "unavailable", "retired", "rebound"]
)
def test_acceptance_rechecks_origin_and_binding_and_never_auto_links(
    database: Database,
    change: str,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            await change_origin(sessions, change)
            with pytest.raises(GitLabReceiptOriginInactive, match="^inactive_origin$"):
                await receiver(sessions).execute(receipt())
            assert await rows(sessions) == []
            if change == "rebound":
                assert (
                    await receiver(sessions).execute(receipt(origin=REPLACEMENT))
                    == InstallationDeliveryDispatchStatus.PENDING
                )
            # The other tenant's project17 cannot supply authority for this origin.
            assert (
                await receiver(sessions).execute(receipt(origin=OTHER))
                == InstallationDeliveryDispatchStatus.PENDING
            )

    asyncio.run(scenario())


def test_payload_cannot_select_origin_and_save_rejects_foreign_provider_or_namespace(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            malicious = (
                PAYLOAD[:-1] + ',"installation":{"id":100},"provider_installation_id":'
                '"00000000-0000-0000-0000-000000000004"}'
            )
            delivery = receipt(payload=malicious)
            await receiver(sessions).execute(delivery)
            persisted = await rows(sessions)
            assert persisted[0]["installation_external_id"] == 99
            assert persisted[0]["gitlab_connection_id"] == str(ORIGIN)
            for invalid in (
                WebhookReceipt(delivery.delivery_id, delivery.event_name, delivery.payload_json),
                WebhookReceipt(
                    delivery.delivery_id,
                    delivery.event_name,
                    delivery.payload_json,
                    "gitlab",
                    OTHER,
                ),
                WebhookReceipt(
                    "raw-id", delivery.event_name, delivery.payload_json, "gitlab", ORIGIN
                ),
            ):
                async with SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions) as uow:
                    with pytest.raises(GitLabReceiptError):
                        await uow.receipts.save(invalid)
            assert await rows(sessions) == persisted

    asyncio.run(scenario())


def test_crash_lease_reclaim_preserves_identity_and_lost_token_cannot_finalize(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            delivery = receipt()
            await receiver(sessions).execute(delivery)

            async def claim(token: UUID, at: datetime) -> WebhookReceipt | None:
                async with SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions) as uow:
                    claimed = await uow.receipts.claim(
                        delivery.delivery_id, token, at, at + timedelta(minutes=5)
                    )
                    await uow.commit()
                    return claimed

            replacement = uuid4()
            owners = await asyncio.gather(claim(TOKEN, NOW), claim(replacement, NOW))
            assert sum(owner is not None for owner in owners) == 1
            first_token = TOKEN if owners[0] is not None else replacement
            next_token = uuid4()
            reclaimed = await claim(next_token, NOW + timedelta(minutes=5))
            assert reclaimed is not None
            assert reclaimed.delivery_id == delivery.delivery_id
            assert reclaimed.provider == "gitlab"
            assert reclaimed.origin_provider_installation_id == ORIGIN
            assert json.loads(reclaimed.payload_json) == {
                "object_kind": "merge_request",
                "project": {"id": 17},
                "object_attributes": {"action": "open"},
            }
            async with SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions) as uow:
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await uow.receipts.mark_projected(delivery.delivery_id, first_token, NOW)
                await uow.receipts.mark_projected(delivery.delivery_id, next_token, NOW)
                await uow.commit()
            assert (await rows(sessions))[0]["projected_at"] is not None

    asyncio.run(scenario())


def test_inactive_origin_disposition_does_not_steal_a_live_foreign_lease(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            delivery = receipt()
            await receiver(sessions).execute(delivery)
            async with SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions) as uow:
                claimed = await uow.receipts.claim(
                    delivery.delivery_id, TOKEN, NOW, NOW + timedelta(minutes=5)
                )
                assert claimed is not None
                assert claimed.delivery_id == delivery.delivery_id
                assert claimed.origin_provider_installation_id == ORIGIN
                await uow.commit()
            async with sessions() as session:
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET intake_enabled = false "
                        "WHERE provider_installation_id = :id"
                    ),
                    {"id": ORIGIN},
                )
                await session.commit()
            before = await rows(sessions)
            async with SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.claim(
                        delivery.delivery_id, uuid4(), NOW, NOW + timedelta(minutes=5)
                    )
                    is None
                )
                await uow.commit()
            assert await rows(sessions) == before
            engine = ReceiveWebhookDelivery(
                provider="gitlab",
                uow_factory=lambda: SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions),
                dispatcher=RecordingDispatcher(),
                now=lambda: NOW + timedelta(minutes=5),
            )
            assert await engine.replay_pending() == 0
            persisted = (await rows(sessions))[0]
            assert persisted["projected_at"] is not None
            assert persisted["projection_claim_token"] is None

    asyncio.run(scenario())


def test_wrong_provider_direct_claim_and_finalize_are_refused_with_exact_token(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            github = WebhookReceipt("github-direct", "ping", "{}")
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.save(github)
                assert (
                    await uow.receipts.claim(
                        github.delivery_id, TOKEN, NOW, NOW + timedelta(minutes=5)
                    )
                    == github
                )
                await uow.commit()
            before = await rows(sessions)
            async with SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.claim(
                        github.delivery_id, uuid4(), NOW, NOW + timedelta(minutes=5)
                    )
                    is None
                )
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await uow.receipts.mark_projected(github.delivery_id, TOKEN, NOW)
                assert await uow.receipts.release(github.delivery_id, TOKEN, NOW, NOW, 3) is False
                assert (
                    await uow.receipts.release_after_dispatch_failure(
                        github.delivery_id, TOKEN, NOW, NOW, 3
                    )
                    is False
                )
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await uow.receipts.retry_run_trigger(
                        github.delivery_id, TOKEN, NOW, now=NOW, received_before=NOW
                    )
                await uow.commit()
            assert await rows(sessions) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["deferred", "failure", "trigger_retry"])
def test_gitlab_engine_preserves_shared_retry_and_terminal_retention(
    database: Database,
    mode: str,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            delivery = receipt()
            await receiver(sessions).execute(delivery)
            clock = [NOW]
            dispatcher = RecordingDispatcher(
                InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER
                if mode == "trigger_retry"
                else InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT,
                fail=mode == "failure",
            )
            engine = ReceiveWebhookDelivery(
                provider="gitlab",
                uow_factory=lambda: SqlAlchemyGitLabWebhookReceiptUnitOfWork(sessions),
                dispatcher=dispatcher,
                now=lambda: clock[0],
            )
            # Anchor the original receipt age independently of the actual test date.
            async with sessions() as session:
                await session.execute(
                    text("UPDATE webhook_events SET received_at = :at"),
                    {"at": NOW},
                )
                await session.commit()
            for attempt in range(3):
                assert await engine.replay_pending() == (0 if mode == "failure" else 1)
                persisted = (await rows(sessions))[0]
                assert persisted["projection_claim_token"] is None
                assert persisted["projection_attempt_count"] == (
                    0 if mode == "trigger_retry" else attempt + 1
                )
                if attempt < 2 or mode == "trigger_retry":
                    assert persisted["retry_after"] is not None
                    assert await engine.replay_pending() == 0
                clock[0] += timedelta(minutes=5) if mode == "deferred" else timedelta(seconds=30)
            if mode == "trigger_retry":
                clock[0] = NOW + timedelta(hours=1)
                assert await engine.replay_pending() == 1
            persisted = (await rows(sessions))[0]
            terminal = "projection_deferred_at" if mode == "deferred" else "projection_failed_at"
            assert persisted[terminal] is not None
            assert persisted["retry_after"] is None
            assert await engine.replay_pending() == 0
            clock[0] = NOW + timedelta(days=32)
            assert await engine.purge_finished() == 1
            assert await rows(sessions) == []

    asyncio.run(scenario())


def test_changed_precise_jsonb_number_is_conflict_not_lossy_float_duplicate(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            first = PAYLOAD[:-1] + ',"value":0.10000000000000001}'
            different = PAYLOAD[:-1] + ',"value":0.1}'
            await receiver(sessions).execute(receipt(payload=first))
            with pytest.raises(GitLabReceiptError, match="^delivery_conflict$"):
                await receiver(sessions).execute(receipt(payload=different))
            async with sessions() as session:
                assert (
                    await session.scalar(text("SELECT payload->>'value' FROM webhook_events"))
                    == "0.10000000000000001"
                )
            dispatcher = RecordingDispatcher()
            assert await receiver(sessions, dispatcher).replay_pending() == 1
            assert json.loads(dispatcher.deliveries[0].payload_json, parse_float=Decimal)[
                "value"
            ] == (Decimal("0.10000000000000001"))

    asyncio.run(scenario())


def test_replay_batch_reports_full_inactive_scan_before_reaching_the_remainder(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            dispatcher = RecordingDispatcher()
            engine = receiver(sessions, dispatcher)
            for number in range(101):
                assert (
                    await engine.execute(receipt(key=f"inactive-batch-{number}"))
                    == InstallationDeliveryDispatchStatus.PENDING
                )
            await change_origin(sessions, "disabled")

            first = await engine.replay_batch()
            assert first.handled_count == 0
            assert first.scanned_count == 100
            assert first.error_count == 0
            assert first.limit == 100
            assert first.full is True
            persisted = await rows(sessions)
            assert len(persisted) == 101
            assert sum(row["projected_at"] is not None for row in persisted) == 100
            assert all(row["projection_claim_token"] is None for row in persisted)
            assert all(row["projection_attempt_count"] == 0 for row in persisted)

            second = await engine.replay_batch()
            assert second.scanned_count == 1
            assert second.handled_count == 0
            assert second.error_count == 0
            assert second.full is False
            assert all(row["projected_at"] is not None for row in await rows(sessions))
            empty = await engine.replay_batch()
            assert empty.scanned_count == 0
            assert empty.handled_count == 0
            assert empty.error_count == 0
            assert empty.full is False
            assert await engine.replay_pending() == 0
            assert dispatcher.deliveries == []

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
