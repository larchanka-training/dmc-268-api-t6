"""`index.repo` against real RabbitMQ and PostgreSQL (AC1, #142).

Opt-in: set ``TEST_RABBITMQ_URL`` (a disposable vhost: the tests delete and redeclare the
index and review topologies; ``RABBITMQ_URL`` in GitHub Actions) and, for the tests that
run a job or the worker, ``TEST_DATABASE_URL`` (disposable PostgreSQL). GitHub is a
``MockTransport``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tarfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue, AbstractRobustConnection
from aiormq.exceptions import ChannelPreconditionFailed
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import app.modules.reviews.infrastructure.amqp as review_amqp
from alembic import command
from app.bootstrap.code_index_worker import CodeIndexSettings, index_consumer
from app.common.infrastructure.heartbeat import is_fresh
from app.modules.code_index.application.build_index import BuildOutcome, BuildStatus
from app.modules.code_index.application.index_store import (
    IndexAdmission,
    IndexBuildStats,
    LeaseClaim,
)
from app.modules.code_index.domain.enums import IndexStatus
from app.modules.code_index.infrastructure.amqp import (
    AmqpIndexWakeups,
    IndexQueuePublisher,
    IndexRepoMessage,
    declare_topology,
    handle_index_delivery,
)
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexUnitOfWork
from app.modules.reviews.application.run_failures import RetryDelays
from app.worker import WorkerSettings, run_worker

SHORT_DELAYS = RetryDelays(
    short=timedelta(milliseconds=200),
    medium=timedelta(milliseconds=300),
    long=timedelta(milliseconds=400),
)
HEAD = "c" * 40
_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    .decode()
)
_INDEX_QUEUES = (
    "code_index.dlq",
    "index.repo",
    "index.repo.retry.30s",
    "index.repo.retry.2m",
    "index.repo.retry.10m",
)
_INDEX_EXCHANGES = ("code_index", "code_index.retry", "code_index.dlx")


async def _reset_topology(url: str) -> None:
    """Delete the index topology and the review one that `run_worker` declares too."""
    connection = await aio_pika.connect(url)
    try:
        channel = await connection.channel()
        names = [*_INDEX_QUEUES, review_amqp.DEAD_LETTER_QUEUE, review_amqp.PUBLISH_QUEUE]
        for engine in review_amqp.ENGINES:
            names.append(review_amqp.run_queue(engine))
            names.extend(review_amqp.retry_queue(key, engine) for key in review_amqp.RETRY_KEYS)
        for name in names:
            await channel.queue_delete(name)
        exchanges = (
            *_INDEX_EXCHANGES,
            review_amqp.EXCHANGE,
            review_amqp.RETRY_EXCHANGE,
            review_amqp.DEAD_LETTER_EXCHANGE,
        )
        for exchange in exchanges:
            await channel.exchange_delete(exchange)
    finally:
        await connection.close()


@pytest.fixture
def broker() -> Iterator[str]:
    url = os.environ.get("TEST_RABBITMQ_URL") or (
        os.environ.get("RABBITMQ_URL") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    )
    if url is None:
        pytest.skip("set TEST_RABBITMQ_URL to run the index queue integration tests")
    asyncio.run(_reset_topology(url))
    try:
        yield url
    finally:
        asyncio.run(_reset_topology(url))


@dataclass(frozen=True)
class Database:
    url: str
    schema: str
    # Repository `a` in installation 18 holds the lease; `b` in installation 17 waits.
    a: UUID
    b: UUID

    @asynccontextmanager
    async def factory(self) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
        engine = create_async_engine(
            self.url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()


def _insert_repository(connection: Any, installation_external_id: int, full_name: str) -> UUID:
    workspace_id, installation_id, repository_id = uuid4(), uuid4(), uuid4()
    statements: list[tuple[str, dict[str, Any]]] = [
        (
            "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)",
            {"id": workspace_id},
        ),
        (
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', :external_id, '{}'::jsonb)",
            {
                "id": installation_id,
                "workspace": workspace_id,
                "external_id": installation_external_id,
            },
        ),
        (
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, :external_id, :full_name, 'main', 'https://github.test')",
            {
                "id": repository_id,
                "installation": installation_id,
                "external_id": installation_external_id * 10,
                "full_name": full_name,
            },
        ),
    ]
    for statement, values in statements:
        connection.execute(text(statement), values)
    return repository_id


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run the index queue integration tests")
    schema = f"test_code_index_queue_{uuid4().hex}"
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                a = _insert_repository(connection, 18, "octo/indexed-a")
                b = _insert_repository(connection, 17, "octo/indexed-b")
                connection.commit()
                yield Database(url, schema, a, b)
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


async def _next(queue: AbstractQueue, timeout: float = 10.0) -> AbstractIncomingMessage:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = await queue.get(no_ack=False, fail=False)
        if message is not None:
            return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no message on {queue.name} within {timeout} s")


async def _until(condition: Callable[[], Awaitable[bool]], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the condition did not hold in time")


async def _count(channel: aio_pika.abc.AbstractChannel, name: str) -> tuple[int, int]:
    queue = await channel.declare_queue(name, passive=True)
    result = queue.declaration_result
    assert result.message_count is not None and result.consumer_count is not None
    return result.message_count, result.consumer_count


# --- topology ---------------------------------------------------------------------


@pytest.mark.integration
def test_the_index_topology_is_declared_with_the_plan_arguments(broker: str) -> None:
    dead_letter = {
        "x-dead-letter-exchange": "code_index",
        "x-dead-letter-routing-key": "index.repo",
    }
    expected: dict[str, dict[str, Any]] = {
        "index.repo": {"x-dead-letter-exchange": "code_index.dlx"},
        "index.repo.retry.30s": {"x-message-ttl": 30_000, **dead_letter},
        "index.repo.retry.2m": {"x-message-ttl": 120_000, **dead_letter},
        "index.repo.retry.10m": {"x-message-ttl": 600_000, **dead_letter},
        "code_index.dlq": {"x-message-ttl": 604_800_000},
    }
    exchanges = {
        "code_index": aio_pika.ExchangeType.DIRECT,
        "code_index.retry": aio_pika.ExchangeType.DIRECT,
        "code_index.dlx": aio_pika.ExchangeType.FANOUT,
    }

    async def scenario() -> list[str]:
        connection = await aio_pika.connect(broker)
        try:
            await declare_topology(await connection.channel(), RetryDelays())
            mismatched: list[str] = []
            for name, arguments in expected.items():
                # Redeclaring with identical arguments succeeds, with others it is a 406.
                await (await connection.channel()).declare_queue(
                    name, durable=True, arguments=arguments
                )
                try:
                    await (await connection.channel()).declare_queue(
                        name, durable=True, arguments={}
                    )
                    mismatched.append(name)
                except ChannelPreconditionFailed:
                    pass
            for name, kind in exchanges.items():
                await (await connection.channel()).declare_exchange(name, kind, durable=True)
                other = (
                    aio_pika.ExchangeType.FANOUT
                    if kind is aio_pika.ExchangeType.DIRECT
                    else aio_pika.ExchangeType.DIRECT
                )
                try:
                    await (await connection.channel()).declare_exchange(name, other, durable=True)
                    mismatched.append(name)
                except ChannelPreconditionFailed:
                    pass
            return mismatched
        finally:
            await connection.close()

    assert asyncio.run(scenario()) == []


# --- retries and the DLQ ----------------------------------------------------------


@pytest.mark.integration
def test_each_retry_queue_returns_the_message_to_index_repo(broker: str) -> None:
    async def scenario() -> list[tuple[str, str, str, bool]]:
        connection = await aio_pika.connect(broker)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await declare_topology(channel, SHORT_DELAYS)
            publisher = await IndexQueuePublisher.open(
                channel, SHORT_DELAYS, lease_waits=2, local_ttl_seconds=1200
            )
            queue = await (await connection.channel()).get_queue("index.repo", ensure=False)
            returned = []
            for key in ("30s", "2m", "10m"):
                message = IndexRepoMessage.wake(uuid4(), 1, datetime.now(UTC))
                await publisher.publish_retry(message, key)
                incoming = await _next(queue)
                await incoming.ack()
                returned.append(
                    (
                        key,
                        incoming.exchange or "",
                        incoming.routing_key or "",
                        json.loads(incoming.body) == message.wire(),
                    )
                )
            return returned
        finally:
            await connection.close()

    assert asyncio.run(scenario()) == [
        ("30s", "code_index", "index.repo", True),
        ("2m", "code_index", "index.repo", True),
        ("10m", "code_index", "index.repo", True),
    ]


@pytest.mark.integration
def test_an_invalid_message_or_a_foreign_id_goes_to_the_dlq(broker: str) -> None:
    wake_up = IndexRepoMessage.wake(uuid4(), 1, datetime.now(UTC)).wire()
    bodies = [
        b"not json",
        json.dumps({**wake_up, "schema": "index.repo/v2"}).encode(),
        json.dumps({**wake_up, "message_id": str(uuid4())}).encode(),
    ]
    calls: list[tuple[UUID, int]] = []

    async def handler(repository_id: UUID, generation: int) -> BuildOutcome:
        calls.append((repository_id, generation))
        return BuildOutcome(BuildStatus.PUBLISHED)

    async def scenario() -> list[bytes]:
        connection = await aio_pika.connect(broker)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await declare_topology(channel, RetryDelays())
            publisher = await IndexQueuePublisher.open(
                channel, RetryDelays(), lease_waits=2, local_ttl_seconds=1200
            )
            exchange = await channel.get_exchange("code_index", ensure=False)
            consuming = await connection.channel()
            queue = await consuming.get_queue("index.repo", ensure=False)
            dlq = await consuming.get_queue("code_index.dlq", ensure=False)
            dead: list[bytes] = []
            for body in bodies:
                await exchange.publish(aio_pika.Message(body, message_id="bad"), "index.repo")
                await handle_index_delivery(await _next(queue), handler, publisher)
                letter = await _next(dlq)
                await letter.ack()
                dead.append(letter.body)
            return dead
        finally:
            await connection.close()

    assert asyncio.run(scenario()) == bodies
    assert calls == []


@pytest.mark.integration
def test_a_copy_into_a_deleted_retry_queue_runs_the_job_three_times_then_dead_letters(
    broker: str,
) -> None:
    # Retry TTLs change by deleting and redeclaring the queues: a worker that still runs
    # in between publishes its copy into nothing, and the broker returns it.
    calls: list[UUID] = []

    async def handler(repository_id: UUID, generation: int) -> BuildOutcome:
        calls.append(repository_id)
        return BuildOutcome(BuildStatus.BUSY)

    async def scenario() -> tuple[bool, bool]:
        connection = await aio_pika.connect(broker)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await declare_topology(channel, RetryDelays())
            publisher = await IndexQueuePublisher.open(
                channel, RetryDelays(), lease_waits=2, local_ttl_seconds=1200
            )
            consuming = await connection.channel()
            await consuming.queue_delete("index.repo.retry.30s")
            queue = await consuming.get_queue("index.repo", ensure=False)
            dlq = await consuming.get_queue("code_index.dlq", ensure=False)
            message = IndexRepoMessage.wake(uuid4(), 1, datetime.now(UTC))
            await (await channel.get_exchange("code_index", ensure=False)).publish(
                aio_pika.Message(
                    json.dumps(message.wire()).encode(), message_id=str(message.message_id)
                ),
                "index.repo",
            )
            for _ in range(3):
                await handle_index_delivery(await _next(queue), handler, publisher)
            letter = await _next(dlq)
            await letter.ack()
            left = await queue.get(no_ack=True, fail=False)
            return json.loads(letter.body) == message.wire(), left is None
        finally:
            await connection.close()

    assert asyncio.run(scenario()) == (True, True)
    assert len(calls) == 3


# --- a job through the queue ------------------------------------------------------


def _tarball(files: dict[str, bytes]) -> bytes:
    """A GitHub-like tarball: a global pax header with the commit, then one root."""
    buffer = io.BytesIO()
    root = f"octo-indexed-b-{HEAD[:7]}"
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": HEAD}
    ) as archive:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for path, content in files.items():
            info = tarfile.TarInfo(f"{root}/{path}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class FakeGitHub:
    """The five requests of a no-policy full build, plus the token exchange."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.tarball = _tarball({"app.py": b"def handler():\n    return 1\n"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append(f"{request.method} {url.host}{url.path}")
        repository = "/repos/octo/indexed-b"
        if url.host == "codeload.github.com":
            return httpx.Response(200, content=self.tarball)
        if url.path == "/app/installations/17/access_tokens":
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        if url.path == repository:
            return httpx.Response(200, json={"default_branch": "main"})
        if url.path == f"{repository}/commits/main":
            return httpx.Response(200, text=HEAD)
        if url.path == f"{repository}/git/trees/{HEAD}":
            return httpx.Response(200, json={"tree": [], "truncated": False})
        if url.path == f"{repository}/tarball/{HEAD}":
            location = f"https://codeload.github.com/octo/indexed-b/legacy.tar.gz/{HEAD}?token=x"
            return httpx.Response(302, headers={"Location": location})
        raise AssertionError(f"unexpected GitHub call {request.method} {url}")


@dataclass(frozen=True)
class Worker:
    code_index: CodeIndexSettings
    github_app_id: str | None = "1"
    github_private_key: str | None = _PEM
    github_api_url: str = "https://api.github.test"


async def _enable(factory: async_sessionmaker[AsyncSession], repository_id: UUID) -> int:
    async with SqlAlchemyCodeIndexUnitOfWork(factory) as uow:
        state = await uow.index.enable(repository_id)
        await uow.commit()
    return state.generation


@pytest.mark.integration
def test_busy_loops_through_the_30s_queue_until_the_admission_frees(
    broker: str, database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    github = FakeGitHub()
    busy = "status=busy retry=index.repo.retry.30s"

    async def scenario() -> tuple[Any, ...]:
        async with database.factory() as factory:
            await _enable(factory, database.a)
            generation = await _enable(factory, database.b)
            # Another job holds the only global slot.
            async with SqlAlchemyCodeIndexUnitOfWork(factory) as uow:
                state = await uow.index.get_state(database.a)
                assert state is not None
                claim = await uow.index.claim_lease(
                    database.a, state.generation, 1_200, IndexAdmission(1, 1, 1 << 30, 1 << 31)
                )
                await uow.commit()
            assert isinstance(claim, LeaseClaim)
            connection: AbstractRobustConnection = await aio_pika.connect_robust(broker)
            try:
                settings = Worker(CodeIndexSettings(max_concurrent=1))
                async with index_consumer(
                    settings, connection, factory, SHORT_DELAYS, httpx.MockTransport(github)
                ) as consumer:
                    assert consumer is not None
                    task = asyncio.create_task(consumer.consume())
                    try:
                        channel = await connection.channel(publisher_confirms=True)
                        await (await AmqpIndexWakeups.open(channel)).wake(database.b, generation)

                        async def looped() -> bool:
                            return caplog.text.count(busy) >= 2

                        await _until(looped)
                        async with SqlAlchemyCodeIndexUnitOfWork(factory) as uow:
                            await uow.index.release(
                                database.a, claim.fence, stats=IndexBuildStats(0, 0, 0)
                            )
                            await uow.commit()

                        async def ready() -> bool:
                            async with SqlAlchemyCodeIndexUnitOfWork(factory) as uow:
                                state = await uow.index.get_state(database.b)
                            return state is not None and state.status is IndexStatus.READY

                        await _until(ready)
                        async with SqlAlchemyCodeIndexUnitOfWork(factory) as uow:
                            built = await uow.index.get_state(database.b)
                        assert built is not None
                        # The ack follows the publication.
                        await asyncio.sleep(0.3)
                        counts = [await _count(channel, name) for name in _INDEX_QUEUES]
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            finally:
                await connection.close()
        return built.indexed_commit_sha, built.file_count, counts

    with caplog.at_level(logging.INFO):
        commit_sha, file_count, counts = asyncio.run(scenario())

    assert (commit_sha, file_count) == (HEAD, 1)
    # Nothing is left: no message in a queue or in the DLQ; index.repo still has a consumer
    # until the end of the scenario.
    assert [messages for messages, _ in counts] == [0, 0, 0, 0, 0]
    # Full: token, repository, HEAD, absent-policy tree, tarball and codeload download.
    assert github.requests == [
        "POST api.github.test/app/installations/17/access_tokens",
        "GET api.github.test/repos/octo/indexed-b",
        "GET api.github.test/repos/octo/indexed-b/commits/main",
        f"GET api.github.test/repos/octo/indexed-b/git/trees/{HEAD}",
        f"GET api.github.test/repos/octo/indexed-b/tarball/{HEAD}",
        f"GET codeload.github.com/octo/indexed-b/legacy.tar.gz/{HEAD}",
    ]


# --- the worker -------------------------------------------------------------------


@pytest.mark.integration
def test_the_heartbeat_stays_fresh_with_three_consumers(
    broker: str, database: Database, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "worker.heartbeat"
    heartbeat.touch()  # left by a previous process: must not count as a beat of this one
    os.utime(heartbeat, (1.0, 1.0))
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": f"{database.url}?options=-csearch_path%3D{database.schema}",
            "RABBITMQ_URL": broker,
            "WORKER_HEARTBEAT_FILE": str(heartbeat),
            "GITHUB_APP_ID": "1",
            "GITHUB_APP_PRIVATE_KEY": _PEM,
            "GITHUB_API_URL": "https://api.github.test",
        }
    )

    def no_github(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected GitHub call {request.method} {request.url}")

    async def scenario() -> tuple[bool, dict[str, int]]:
        task = asyncio.create_task(
            run_worker(settings, leader_period=0.1, github_transport=httpx.MockTransport(no_github))
        )
        try:
            await asyncio.sleep(1.5)
            healthy = not task.done() and is_fresh(heartbeat, max_age=5, now=time.time())
            connection = await aio_pika.connect(broker)
            try:
                channel = await connection.channel()
                names = ("review.run.fast", "review.publish", "index.repo")
                consumers = {name: (await _count(channel, name))[1] for name in names}
            finally:
                await connection.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return healthy, consumers

    assert asyncio.run(scenario()) == (
        True,
        {"review.run.fast": 1, "review.publish": 1, "index.repo": 1},
    )
