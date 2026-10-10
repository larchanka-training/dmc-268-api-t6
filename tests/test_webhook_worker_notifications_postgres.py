"""Real PostgreSQL queue wake-up, recovery and claim guarantees (#129)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.webhook_work_listener import listen_forever
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    WebhookReceipt,
)
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.workspaces.application.link_github_installations import LinkGitHubInstallations
from app.webhook_worker import sweep_forever
from tests.test_webhook_work_notifications_postgres import (
    DEFERRED_AT,
    RETRY_AT,
    ControlledLinkUnitOfWork,
    Database,
    LinkedInstallationGitHub,
    ReconciliationControl,
    migrated_database,
    receipt_retry_state,
    seed_deferred_receipt,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def database() -> Iterator[Database]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with migrated_database(database_url) as isolated_database:
        yield isolated_database


class Wake(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.hints: asyncio.Queue[None] = asyncio.Queue()
        self.waiting: asyncio.Queue[None] = asyncio.Queue()
        self.drop = False

    def set(self) -> None:
        self.hints.put_nowait(None)
        if not self.drop:
            super().set()

    async def wait(self) -> Literal[True]:
        self.waiting.put_nowait(None)
        return await super().wait()


class Dispatcher:
    """Fake GitHub dispatch port; durable receipt/claim/finalization remain real."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.block_id: str | None = None
        self.release = asyncio.Event()
        self.blocked = asyncio.Event()

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
        self.calls.append(delivery.delivery_id)
        self.started.put_nowait(delivery.delivery_id)
        if delivery.delivery_id == self.block_id:
            self.blocked.set()
            await self.release.wait()
        return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)


@dataclass
class Worker:
    factory: async_sessionmaker[AsyncSession]
    receiver: ReceiveGitHubDelivery
    dispatcher: Dispatcher
    wake: Wake
    application_name: str

    async def backend_pid(self) -> int | None:
        async with self.factory() as session:
            return cast(
                int | None,
                await session.scalar(
                    text("SELECT pid FROM pg_stat_activity WHERE application_name = :name"),
                    {"name": self.application_name},
                ),
            )

    async def projected(self, delivery_id: str) -> bool | None:
        async with self.factory() as session:
            return cast(
                bool | None,
                await session.scalar(
                    text(
                        "SELECT projected_at IS NOT NULL FROM webhook_events "
                        "WHERE delivery_id = :id"
                    ),
                    {"id": delivery_id},
                ),
            )

    async def wait_projected(self, delivery_id: str) -> None:
        async with asyncio.timeout(5):
            while await self.projected(delivery_id) is not True:
                await asyncio.sleep(0)

    async def hint_and_scan(self) -> None:
        while not self.wake.waiting.empty():
            self.wake.waiting.get_nowait()
        async with self.factory() as session, session.begin():
            await session.execute(text("SELECT pg_notify('webhook_work_available', '')"))
        await asyncio.wait_for(self.wake.waiting.get(), timeout=5)


@asynccontextmanager
async def worker(
    database: Database,
    *,
    dispatcher: Dispatcher | None = None,
    listener: bool = True,
    poll_interval: float = 30,
    clock: list[datetime] | None = None,
) -> AsyncIterator[Worker]:
    engine = database.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dispatcher = Dispatcher() if dispatcher is None else dispatcher
    now = (lambda: clock[0]) if clock is not None else lambda: datetime.now(UTC)
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory),
        dispatcher=dispatcher,
        now=now,
    )
    reviver = ReviveDeferredInstallationDeliveries(
        uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory), now=now
    )
    wake = Wake()
    application_name = f"test_webhook_listener_{uuid4().hex}"
    listener_url = make_url(database.url).update_query_dict({"application_name": application_name})
    tasks: list[asyncio.Task[object]] = []
    try:
        if listener:
            tasks.append(
                asyncio.create_task(listen_forever(listener_url.render_as_string(False), wake))
            )
            # Production LISTEN completion itself supplies readiness; no startup sleep.
            await asyncio.wait_for(wake.hints.get(), timeout=5)
        tasks.append(
            asyncio.create_task(sweep_forever(receiver, reviver, wake, poll_interval=poll_interval))
        )
        await asyncio.wait_for(wake.waiting.get(), timeout=5)
        yield Worker(factory, receiver, dispatcher, wake, application_name)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await engine.dispose()


def delivery(delivery_id: str | None = None) -> WebhookReceipt:
    return WebhookReceipt(delivery_id or uuid4().hex, "installation", '{"installation":{"id":99}}')


def test_insert_commit_wakes_idle_worker_but_uncommitted_receipt_cannot_dispatch(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with worker(database) as running:
            receipt = delivery()
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(running.factory) as uow:
                assert await uow.receipts.save(receipt) is True
                await running.hint_and_scan()
                assert running.dispatcher.calls == []
                assert await running.projected(receipt.delivery_id) is None
                await uow.commit()
            await running.wait_projected(receipt.delivery_id)
            assert running.dispatcher.calls == [receipt.delivery_id]

    asyncio.run(scenario())


def test_insert_rollback_never_dispatches_even_after_an_explicit_queue_scan(
    database: Database,
) -> None:
    async def scenario() -> None:
        async with worker(database) as running:
            receipt = delivery()
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(running.factory) as uow:
                assert await uow.receipts.save(receipt) is True
                await running.hint_and_scan()
                assert running.dispatcher.calls == []
                await uow.rollback()
            await running.hint_and_scan()
            assert running.dispatcher.calls == []
            assert await running.projected(receipt.delivery_id) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("rollback", [False, True])
def test_oauth_commit_wakes_worker_while_rollback_preserves_deferred_receipt(
    database: Database, rollback: bool
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        receipt_id = uuid4().hex
        try:
            await seed_deferred_receipt(factory, receipt_id)
            async with worker(database) as running:
                control = ReconciliationControl(rollback=rollback)
                use_case = LinkGitHubInstallations(
                    github=LinkedInstallationGitHub(control),
                    uow_factory=lambda: ControlledLinkUnitOfWork(running.factory, control),
                )
                task = asyncio.create_task(use_case.execute("synthetic-token"))
                try:
                    await asyncio.wait_for(control.before_commit.wait(), timeout=5)
                    await running.hint_and_scan()
                    assert running.dispatcher.calls == []
                    assert await receipt_retry_state(factory, receipt_id) == (
                        3,
                        DEFERRED_AT,
                        RETRY_AT,
                    )
                    control.release_commit.set()
                    if rollback:
                        with pytest.raises(RuntimeError, match="test reconciliation rollback"):
                            await asyncio.wait_for(task, timeout=5)
                        await running.hint_and_scan()
                        assert running.dispatcher.calls == []
                        assert await receipt_retry_state(factory, receipt_id) == (
                            3,
                            DEFERRED_AT,
                            RETRY_AT,
                        )
                    else:
                        assert len(await asyncio.wait_for(task, timeout=5)) == 1
                        await running.wait_projected(receipt_id)
                        assert running.dispatcher.calls == [receipt_id]
                finally:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_startup_backlog_is_processed_and_shutdown_closes_actual_listener(
    database: Database,
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        receipt = delivery()
        try:
            receiver = ReceiveGitHubDelivery(
                uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory)
            )
            await receiver.execute(receipt)
            async with worker(database) as running:
                await running.wait_projected(receipt.delivery_id)
                assert running.dispatcher.calls == [receipt.delivery_id]
                assert await running.backend_pid() is not None
                name = running.application_name
            async with factory() as observer:
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name"
                        ),
                        {"name": name},
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_actual_connection_loss_and_offline_insert_recover_after_reconnect(
    database: Database,
) -> None:
    async def scenario() -> None:
        disconnected = asyncio.Event()

        class DisconnectHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if "listener lost its connection" in record.getMessage():
                    disconnected.set()

        logger = logging.getLogger("app.bootstrap.webhook_work_listener")
        handler = DisconnectHandler()
        logger.addHandler(handler)
        try:
            async with worker(database) as running:
                original_pid = await running.backend_pid()
                assert original_pid is not None
                async with running.factory() as session, session.begin():
                    assert (
                        await session.scalar(
                            text("SELECT pg_terminate_backend(:pid)"), {"pid": original_pid}
                        )
                        is True
                    )
                await asyncio.wait_for(disconnected.wait(), timeout=5)
                assert await running.backend_pid() is None
                receipt = delivery()
                await running.receiver.execute(receipt)
                # Receipt committed during the real listener's five-second reconnect backoff.
                assert await running.backend_pid() is None
                assert running.dispatcher.calls == []
                await asyncio.wait_for(running.wake.hints.get(), timeout=8)
                new_pid = await running.backend_pid()
                assert new_pid is not None and new_pid != original_pid
                await running.wait_projected(receipt.delivery_id)
                assert running.dispatcher.calls == [receipt.delivery_id]
        finally:
            logger.removeHandler(handler)

    asyncio.run(scenario())


def test_missed_notification_is_recovered_by_timeout_and_future_due_retry(
    database: Database,
) -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        clock = [now]
        async with worker(database, poll_interval=0.5, clock=clock) as running:
            running.wake.drop = True
            receipt = delivery()
            await running.receiver.execute(receipt)
            await asyncio.wait_for(running.wake.hints.get(), timeout=5)
            assert running.wake.is_set() is False
            await running.wait_projected(receipt.delivery_id)
            retry = delivery()
            async with running.factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO webhook_events (id, delivery_id, event, payload, "
                        "installation_external_id, projection_attempt_count, retry_after) "
                        "VALUES (gen_random_uuid(), :id, 'installation', "
                        "CAST(:payload AS jsonb), 99, 1, :later)"
                    ),
                    {
                        "later": now + timedelta(seconds=30),
                        "id": retry.delivery_id,
                        "payload": retry.payload_json,
                    },
                )
            while not running.wake.waiting.empty():
                running.wake.waiting.get_nowait()
            await asyncio.wait_for(running.wake.waiting.get(), timeout=5)
            assert await running.projected(retry.delivery_id) is False
            clock[0] = now + timedelta(seconds=31)
            await running.wait_projected(retry.delivery_id)
            assert running.dispatcher.calls == [receipt.delivery_id, retry.delivery_id]

    asyncio.run(scenario())


def test_receipt_arriving_during_dispatch_survives_the_next_wait(database: Database) -> None:
    async def scenario() -> None:
        dispatcher = Dispatcher()
        first, second = delivery(), delivery()
        dispatcher.block_id = first.delivery_id
        async with worker(database, dispatcher=dispatcher) as running:
            await running.receiver.execute(first)
            assert await asyncio.wait_for(dispatcher.started.get(), timeout=5) == first.delivery_id
            while not running.wake.hints.empty():
                running.wake.hints.get_nowait()
            await running.receiver.execute(second)
            await asyncio.wait_for(running.wake.hints.get(), timeout=5)
            dispatcher.release.set()
            await running.wait_projected(first.delivery_id)
            await running.wait_projected(second.delivery_id)
            assert dispatcher.calls == [first.delivery_id, second.delivery_id]

    asyncio.run(scenario())


def test_two_workers_and_repeated_hints_keep_claim_and_dispatch_idempotent(
    database: Database,
) -> None:
    async def scenario() -> None:
        dispatcher = Dispatcher()
        receipt = delivery()
        dispatcher.block_id = receipt.delivery_id
        other = Dispatcher()
        other.calls = dispatcher.calls
        other.started = dispatcher.started
        other.release = dispatcher.release
        other.block_id = receipt.delivery_id
        async with (
            worker(database, dispatcher=dispatcher) as first,
            worker(database, dispatcher=other) as second,
        ):
            await first.receiver.execute(receipt)
            assert (
                await asyncio.wait_for(dispatcher.started.get(), timeout=5) == receipt.delivery_id
            )
            passive = second if dispatcher.blocked.is_set() else first
            for _ in range(3):
                await passive.hint_and_scan()
            assert dispatcher.calls == [receipt.delivery_id]
            async with second.factory() as observer:
                assert (
                    await observer.scalar(
                        text(
                            "SELECT projection_claim_token IS NOT NULL "
                            "AND projection_lease_until > now() "
                            "FROM webhook_events WHERE delivery_id = :id"
                        ),
                        {"id": receipt.delivery_id},
                    )
                    is True
                )
            dispatcher.release.set()
            await first.wait_projected(receipt.delivery_id)
            await first.hint_and_scan()
            await second.hint_and_scan()
            assert dispatcher.calls == [receipt.delivery_id]

    asyncio.run(scenario())
