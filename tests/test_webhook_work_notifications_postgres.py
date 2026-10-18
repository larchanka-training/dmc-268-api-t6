"""Webhook queue hints obey receipt transactions (#129), on real PostgreSQL."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.workspaces.application.link_github_installations import (
    AuthenticatedGitHubInstallations,
    GitHubInstallation,
    LinkGitHubInstallations,
)
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Database:
    url: str
    schema: str

    def engine(self) -> AsyncEngine:
        return create_async_engine(
            self.url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )


@pytest.fixture
def database() -> Iterator[Database]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with migrated_database(database_url) as isolated_database:
        yield isolated_database


@contextmanager
def migrated_database(database_url: str) -> Iterator[Database]:
    """Provide an isolated migrated schema; callers decide whether a DB is configured."""
    schema = f"test_webhook_notify_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            try:
                yield Database(database_url, schema)
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


@contextmanager
def recorded_backends() -> Iterator[set[int]]:
    """Record the backend of every psycopg connection this process opens.

    LISTEN is database-wide, so a hint from a pid outside the set came from another process
    sharing the database. SQLAlchemy looks the driver's ``connect`` up per connection, so
    engine sessions are recorded too; wrapping the current attribute composes with test patches.
    """
    backends: set[int] = set()
    connect = psycopg.AsyncConnection.connect

    async def recording_connect(
        conninfo: str = "", **kwargs: Any
    ) -> psycopg.AsyncConnection[tuple[Any, ...]]:
        connection = await connect(conninfo, **kwargs)
        backends.add(connection.info.backend_pid)
        return connection

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(psycopg.AsyncConnection, "connect", recording_connect)
        yield backends


@pytest.fixture
def own_backends() -> Iterator[set[int]]:
    with recorded_backends() as backends:
        yield backends


@asynccontextmanager
async def listen(database: Database) -> AsyncIterator[asyncio.Queue[psycopg.Notify]]:
    conninfo = (
        make_url(database.url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as connection:
        # Completion is the readiness barrier; no startup sleep or transaction around LISTEN.
        await connection.execute("LISTEN webhook_work_available")
        notifications: asyncio.Queue[psycopg.Notify] = asyncio.Queue()

        async def collect() -> None:
            async for notification in connection.notifies():
                notifications.put_nowait(notification)

        task = asyncio.create_task(collect())
        try:
            yield notifications
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def expect_signal(
    notifications: asyncio.Queue[psycopg.Notify], own: set[int], writer_pid: int
) -> None:
    """The first hint from this process must come from the writer's own transaction."""
    # One deadline for the whole wait: steady foreign traffic cannot stretch it.
    async with asyncio.timeout(5):
        while True:
            signal = await notifications.get()
            if signal.pid in own:
                break
    assert (signal.channel, signal.payload, signal.pid) == (
        "webhook_work_available",
        "",
        writer_pid,
    )


async def assert_no_signal(notifications: asyncio.Queue[psycopg.Notify], own: set[int]) -> None:
    """No hint from any backend of this process; other processes' hints are skipped."""
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            while True:
                signal = await notifications.get()
                assert signal.pid not in own


async def pending_ids(factory: async_sessionmaker[AsyncSession]) -> tuple[str, ...]:
    async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as observer:
        return await observer.receipts.pending_ids(datetime.now(UTC), limit=100)


def test_new_receipt_signals_only_after_commit(database: Database, own_backends: set[int]) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        delivery_id = uuid4().hex
        delivery = WebhookReceipt(delivery_id, "installation", '{"installation":{"id":99}}')
        try:
            async with listen(database) as notifications:
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as uow:
                    assert await uow.receipts.save(delivery) is True
                    writer_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
                    await assert_no_signal(notifications, own_backends)
                    assert await pending_ids(factory) == ()
                    await uow.commit()
                await expect_signal(notifications, own_backends, writer_pid)
                assert await pending_ids(factory) == (delivery_id,)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_rolled_back_receipt_never_signals_or_becomes_visible(
    database: Database, own_backends: set[int]
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        delivery = WebhookReceipt(uuid4().hex, "installation", '{"installation":{"id":99}}')
        try:
            async with listen(database) as notifications:
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as uow:
                    assert await uow.receipts.save(delivery) is True
                    await assert_no_signal(notifications, own_backends)
                    assert await pending_ids(factory) == ()
                    await uow.rollback()
                await assert_no_signal(notifications, own_backends)
                assert await pending_ids(factory) == ()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_duplicate_receipt_does_not_send_another_signal(
    database: Database, own_backends: set[int]
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        delivery_id = uuid4().hex
        delivery = WebhookReceipt(delivery_id, "installation", '{"installation":{"id":99}}')
        try:
            async with listen(database) as notifications:
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as uow:
                    assert await uow.receipts.save(delivery) is True
                    writer_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
                    await uow.commit()
                await expect_signal(notifications, own_backends, writer_pid)
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as uow:
                    assert await uow.receipts.save(delivery) is False
                    await uow.commit()
                await assert_no_signal(notifications, own_backends)
                assert await pending_ids(factory) == (delivery_id,)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@dataclass
class ReconciliationControl:
    before_commit: asyncio.Event = field(default_factory=asyncio.Event)
    release_commit: asyncio.Event = field(default_factory=asyncio.Event)
    active: bool = False
    commits: int = 0
    rollback: bool = False
    writer_pid: int | None = None


class ControlledLinkUnitOfWork(SqlAlchemyGitHubInstallationLinkUnitOfWork):
    """Real database UoW with a barrier at the application's final commit boundary."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], control: ReconciliationControl
    ) -> None:
        super().__init__(factory)
        self.control = control

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.control.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await super().__aexit__(exc_type, exc, traceback)
        finally:
            self.control.active = False

    async def commit(self) -> None:
        self.control.commits += 1
        if self.control.commits == 2:
            # The second commit is the apply transaction, the only one that may send the hint.
            self.control.writer_pid = await self.session.scalar(text("SELECT pg_backend_pid()"))
            self.control.before_commit.set()
            await self.control.release_commit.wait()
            if self.control.rollback:
                raise RuntimeError("test reconciliation rollback")
        await super().commit()


class LinkedInstallationGitHub:
    """Fake GitHub boundary; both network reads require the real UoW to be closed."""

    def __init__(self, control: ReconciliationControl) -> None:
        self.control = control

    async def identify_user(self, access_token: str) -> int:
        assert self.control.active is False
        return 41

    async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
        assert self.control.active is False
        return AuthenticatedGitHubInstallations(41, (GitHubInstallation(99, "octo", (101,)),))


DEFERRED_AT = datetime(2026, 10, 5, 12, tzinfo=UTC)
RETRY_AT = datetime(2026, 10, 5, 13, tzinfo=UTC)


async def seed_deferred_receipt(
    factory: async_sessionmaker[AsyncSession], delivery_id: str
) -> None:
    async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory) as uow:
        assert (
            await uow.receipts.save(
                WebhookReceipt(delivery_id, "pull_request", '{"installation":{"id":99}}')
            )
            is True
        )
        await uow.commit()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE webhook_events SET projection_attempt_count = 3, "
                "projection_deferred_at = :deferred, retry_after = :retry "
                "WHERE delivery_id = :delivery_id"
            ),
            {"deferred": DEFERRED_AT, "retry": RETRY_AT, "delivery_id": delivery_id},
        )


async def receipt_retry_state(
    factory: async_sessionmaker[AsyncSession], delivery_id: str
) -> tuple[int, datetime | None, datetime | None]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT projection_attempt_count, projection_deferred_at, retry_after "
                    "FROM webhook_events WHERE delivery_id = :delivery_id"
                ),
                {"delivery_id": delivery_id},
            )
        ).one()
        return row[0], row[1], row[2]


def test_oauth_reconciliation_signals_only_when_revived_receipt_commits(
    database: Database, own_backends: set[int]
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        delivery_id = uuid4().hex
        control = ReconciliationControl()
        use_case = LinkGitHubInstallations(
            github=LinkedInstallationGitHub(control),
            uow_factory=lambda: ControlledLinkUnitOfWork(factory, control),
        )
        task: asyncio.Task[tuple[UUID, ...]] | None = None
        try:
            await seed_deferred_receipt(factory, delivery_id)
            async with listen(database) as notifications:
                task = asyncio.create_task(use_case.execute("synthetic-token"))
                await asyncio.wait_for(control.before_commit.wait(), timeout=5)
                await assert_no_signal(notifications, own_backends)
                assert await receipt_retry_state(factory, delivery_id) == (3, DEFERRED_AT, RETRY_AT)
                control.release_commit.set()
                workspaces = await asyncio.wait_for(task, timeout=5)
                assert len(workspaces) == 1
                assert control.writer_pid is not None
                await expect_signal(notifications, own_backends, control.writer_pid)
                assert await receipt_retry_state(factory, delivery_id) == (0, None, None)
                assert await pending_ids(factory) == (delivery_id,)
        finally:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await engine.dispose()

    asyncio.run(scenario())


def test_rolled_back_oauth_reconciliation_retains_deferral_and_sends_no_signal(
    database: Database, own_backends: set[int]
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        delivery_id = uuid4().hex
        control = ReconciliationControl(rollback=True)
        use_case = LinkGitHubInstallations(
            github=LinkedInstallationGitHub(control),
            uow_factory=lambda: ControlledLinkUnitOfWork(factory, control),
        )
        task: asyncio.Task[tuple[UUID, ...]] | None = None
        try:
            await seed_deferred_receipt(factory, delivery_id)
            async with listen(database) as notifications:
                task = asyncio.create_task(use_case.execute("synthetic-token"))
                await asyncio.wait_for(control.before_commit.wait(), timeout=5)
                await assert_no_signal(notifications, own_backends)
                assert await receipt_retry_state(factory, delivery_id) == (3, DEFERRED_AT, RETRY_AT)
                control.release_commit.set()
                with pytest.raises(RuntimeError, match="test reconciliation rollback"):
                    await asyncio.wait_for(task, timeout=5)
                await assert_no_signal(notifications, own_backends)
                assert await receipt_retry_state(factory, delivery_id) == (3, DEFERRED_AT, RETRY_AT)
                assert await pending_ids(factory) == ()
                async with factory() as observer:
                    assert await observer.scalar(text("SELECT count(*) FROM workspaces")) == 0
                    assert (
                        await observer.scalar(
                            text("SELECT applied_generation FROM github_user_installation_sync")
                        )
                        == 0
                    )
        finally:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await engine.dispose()

    asyncio.run(scenario())


def test_oauth_reconciliation_with_no_matching_receipt_sends_no_signal(
    database: Database, own_backends: set[int]
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        control = ReconciliationControl()
        control.release_commit.set()
        use_case = LinkGitHubInstallations(
            github=LinkedInstallationGitHub(control),
            uow_factory=lambda: ControlledLinkUnitOfWork(factory, control),
        )
        try:
            async with listen(database) as notifications:
                assert len(await use_case.execute("synthetic-token")) == 1
                await assert_no_signal(notifications, own_backends)
                assert await pending_ids(factory) == ()
                async with factory() as observer:
                    assert (
                        await observer.scalar(
                            text("SELECT applied_generation FROM github_user_installation_sync")
                        )
                        == 1
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_by", ["failure", "lease"])
def test_oauth_wake_preserves_permanent_failure_and_claim_ownership(
    database: Database, blocked_by: str
) -> None:
    async def scenario() -> None:
        engine = database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        delivery_id = uuid4().hex
        claim = uuid4() if blocked_by == "lease" else None
        lease = datetime(2099, 10, 5, 12, tzinfo=UTC) if blocked_by == "lease" else None
        failed = DEFERRED_AT if blocked_by == "failure" else None
        control = ReconciliationControl()
        control.release_commit.set()
        use_case = LinkGitHubInstallations(
            github=LinkedInstallationGitHub(control),
            uow_factory=lambda: ControlledLinkUnitOfWork(factory, control),
        )
        try:
            await seed_deferred_receipt(factory, delivery_id)
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE webhook_events SET projection_failed_at = :failed, "
                        "projection_claim_token = :claim, projection_lease_until = :lease "
                        "WHERE delivery_id = :delivery_id"
                    ),
                    {"failed": failed, "claim": claim, "lease": lease, "delivery_id": delivery_id},
                )
            assert len(await use_case.execute("synthetic-token")) == 1
            async with factory() as observer:
                row = (
                    await observer.execute(
                        text(
                            "SELECT projection_failed_at, projection_claim_token, "
                            "projection_lease_until FROM webhook_events "
                            "WHERE delivery_id = :delivery_id"
                        ),
                        {"delivery_id": delivery_id},
                    )
                ).one()
                assert tuple(row) == (failed, claim, lease)
            assert await receipt_retry_state(factory, delivery_id) == (0, None, None)
            assert await pending_ids(factory) == ()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
