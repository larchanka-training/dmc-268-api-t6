"""A shared Bitbucket push outage reaches a bounded terminal receipt state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.modules.integrations.bitbucket.api.dispatch import BitbucketWebhookDispatchAdapter
from app.modules.integrations.bitbucket.application.provider_error import BitbucketProviderError
from app.modules.integrations.bitbucket.application.webhook_receipts import (
    AuthenticatedBitbucketOrigin,
    prepare_bitbucket_receipt,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.bitbucket.infrastructure.webhook_receipts import (
    SqlAlchemyBitbucketWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveWebhookDelivery,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult
from tests.bitbucket_connection_postgres_support import _config
from tests.bitbucket_projection_support import seed_confirmed_bitbucket_repository

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("reason", "retry_after", "delay"),
    [
        ("provider_unavailable", None, timedelta(seconds=30)),
        ("provider_rate_limited", 120, timedelta(seconds=120)),
    ],
)
def test_shared_push_failure_expires_after_six_hours_without_burning_attempts(
    bitbucket_connection_schema: Connection,
    caplog: pytest.LogCaptureFixture,
    reason: str,
    retry_after: int | None,
    delay: timedelta,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo = (uuid4() for _ in range(4))
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    start = datetime.now(UTC).replace(microsecond=0)
    clock = [start]
    caplog.set_level(logging.WARNING)

    class RepositoryFailure:
        calls = 0

        async def pull_request(self, *_: object) -> EnqueueResult | None:
            raise AssertionError("push receipt must not request an individual PR")

        async def push(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            branches: frozenset[str],
            binding_generation: int,
            webhook_generation: int,
            delivery_id: str,
        ) -> tuple[EnqueueResult, ...]:
            assert installation_id == installation
            assert repository_uuid == remote_repo
            assert branches == frozenset({"feature"})
            assert binding_generation > 0 and webhook_generation > 0
            assert repository_id and delivery_id
            self.calls += 1
            raise BitbucketProviderError(reason, retry_after=retry_after)

    handler = RepositoryFailure()
    receiver = ReceiveWebhookDelivery(
        provider="bitbucket",
        uow_factory=lambda: SqlAlchemyBitbucketWebhookReceiptUnitOfWork(sessions),
        dispatcher=BitbucketWebhookDispatchAdapter(handler),
        now=lambda: clock[0],
    )

    async def state(delivery_id: str) -> tuple[datetime | None, datetime | None, int, int | None]:
        async with sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT projection_failed_at,retry_after,projection_attempt_count,"
                        "bitbucket_push_after_pr_number FROM webhook_events "
                        "WHERE delivery_id=:delivery"
                    ),
                    {"delivery": delivery_id},
                )
            ).one()
            return row[0], row[1], row[2], row[3]

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                repository = await seed_confirmed_bitbucket_repository(
                    session,
                    workspace=workspace,
                    installation=installation,
                    remote_workspace=remote_workspace,
                    remote_repo=remote_repo,
                    cipher=cipher,
                )
                generations = (
                    await session.execute(
                        text(
                            "SELECT binding_generation,generation "
                            "FROM bitbucket_repository_webhook_secrets "
                            "WHERE repository_id=:repository"
                        ),
                        {"repository": repository},
                    )
                ).one()
            receipt = prepare_bitbucket_receipt(
                origin=AuthenticatedBitbucketOrigin(
                    installation,
                    repository,
                    workspace,
                    remote_repo,
                    generations[0],
                    generations[1],
                ),
                event_name="repo:push",
                hook_uuid=uuid4(),
                request_uuid=uuid4(),
                payload_json=json.dumps(
                    {
                        "repository": {"uuid": "{" + str(remote_repo) + "}"},
                        "push": {"changes": [{"new": {"type": "branch", "name": "feature"}}]},
                    }
                ),
            )
            assert await receiver.execute(receipt) == InstallationDeliveryDispatchStatus.PENDING
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE webhook_events SET received_at=:received WHERE delivery_id=:id"),
                    {"received": start, "id": receipt.delivery_id},
                )

            first = await receiver.replay_batch()
            assert (first.scanned_count, first.handled_count, first.error_count) == (1, 1, 0)
            assert await state(receipt.delivery_id) == (None, start + delay, 0, None)
            clock[0] = start + delay - timedelta(seconds=1)
            assert (await receiver.replay_batch()).scanned_count == 0
            clock[0] = start + delay
            assert (await receiver.replay_batch()).handled_count == 1
            assert await state(receipt.delivery_id) == (None, clock[0] + delay, 0, None)

            clock[0] = start + timedelta(hours=5, minutes=58)
            within = await receiver.replay_batch()
            assert (within.scanned_count, within.handled_count, within.error_count) == (1, 1, 0)
            assert await state(receipt.delivery_id) == (None, clock[0] + delay, 0, None)

            clock[0] = start + timedelta(hours=6)
            terminal = await receiver.replay_batch()
            assert (terminal.scanned_count, terminal.handled_count, terminal.error_count) == (
                1,
                1,
                0,
            )
            assert await state(receipt.delivery_id) == (clock[0], None, 0, None)
            assert handler.calls == 4
            warnings = [
                record.getMessage()
                for record in caplog.records
                if "reason=provider_retry_expired" in record.getMessage()
            ]
            assert warnings == [
                f"Bitbucket webhook delivery {receipt.delivery_id} "
                "failed reason=provider_retry_expired"
            ]
            assert "test-token" not in caplog.text

            clock[0] = start + timedelta(days=1)
            assert (await receiver.replay_batch()).scanned_count == 0
            async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.claim(
                        receipt.delivery_id, uuid4(), clock[0], clock[0] + timedelta(minutes=5)
                    )
                    is None
                )
            assert handler.calls == 4
        finally:
            await engine.dispose()

    asyncio.run(scenario())
