"""Broken deliveries and persisted pointers against real RabbitMQ and PostgreSQL.

Opt-in: set ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL``
(disposable RabbitMQ: the tests delete and redeclare the review topology).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
from app.modules.reviews.application.queue_messages import StoredRunMessage
from app.modules.reviews.application.run_delivery import HandleRunDelivery, RunDeliveryPointer
from app.modules.reviews.infrastructure.amqp import (
    DEAD_LETTER_QUEUE,
    EXCHANGE,
    amqp_channels,
    handle_run_delivery,
    run_queue,
)
from app.modules.reviews.infrastructure.run_delivery import SqlAlchemyRunDeliveryReader
from app.worker import WorkerSettings, run_worker
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import rig
from tests.test_run_delivery_postgres import seeded
from tests.worker_queue_postgres import (
    BASE,
    HEAD,
    SHORT_DELAYS,
    Env,
    Model,
    _reset_topology,
    run_state,
)

pytest_plugins = ("tests.worker_queue_postgres",)

guard_database = projection.authority_database


@pytest.mark.integration
def test_repeated_delivery_failures_route_to_dlq_across_reconnects(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS",
        0.0,
    )

    run_id = uuid4()
    body = {
        "schema": "review.run/v1",
        "message_id": str(run_id),
        "run_id": str(run_id),
        "workspace_id": str(uuid4()),
        "installation_id": 17,
        "repo": {
            "id": str(uuid4()),
            "provider": "github",
            "external_id": 101,
            "full_name": "octo/repo",
        },
        "pr": {
            "number": 7,
            "head_sha": HEAD,
            "base_sha": BASE,
            "base_ref": "main",
        },
        "engine": "fast",
        "rule_version_id": str(env.rule_id),
        "prompt_version_id": str(env.prompt_id),
        "trigger": "webhook",
        "attempt": 1,
        "requested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    async def publish_initial() -> None:
        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            connection = channels.connection
            channel = await connection.channel()
            exchange = await channel.get_exchange(EXCHANGE, ensure=False)
            await exchange.publish(
                aio_pika.Message(
                    json.dumps(body).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=str(run_id),
                ),
                routing_key=run_queue("fast"),
            )

    asyncio.run(publish_initial())

    deliveries_seen = 0

    async def deliver_once_and_fail() -> None:
        nonlocal deliveries_seen
        from app.modules.reviews.infrastructure.amqp import _unexpected_delivery_attempts

        _unexpected_delivery_attempts.clear()

        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            queue = await channels.consumer_queue(run_queue("fast"))
            async with queue.iterator() as messages:

                async def _consume() -> None:
                    nonlocal deliveries_seen
                    async for message in messages:
                        deliveries_seen += 1

                        async def failing_handler(_: RunDeliveryPointer) -> DeliveryOutcome:
                            raise RuntimeError("handler failure")

                        await handle_run_delivery(
                            message,
                            handler=failing_handler,
                            publisher=channels.publisher,
                        )
                        break

                await asyncio.wait_for(_consume(), timeout=10.0)

    for _ in range(3):
        asyncio.run(deliver_once_and_fail())

    assert deliveries_seen == 3

    async def verify_dlq() -> tuple[int, dict[str, Any] | None]:
        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            probe = await channels.connection.channel()
            fast_q = await probe.declare_queue(run_queue("fast"), passive=True)
            fast_count = fast_q.declaration_result.message_count
            dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)

            dlq_msg: aio_pika.abc.AbstractIncomingMessage | None = None
            async with dlq.iterator() as messages:

                async def _consume_dlq() -> None:
                    nonlocal dlq_msg
                    async for msg in messages:
                        await msg.ack()
                        dlq_msg = msg
                        break

                await asyncio.wait_for(_consume_dlq(), timeout=10.0)
            dlq_headers = dict(dlq_msg.headers or {}) if dlq_msg is not None else None
            return int(fast_count or 0), dlq_headers

    fast_count, dlq_headers = asyncio.run(verify_dlq())
    assert fast_count == 0
    assert dlq_headers is not None
    assert dlq_headers.get("x-attempt") == 2


@pytest.fixture
def guarded_queue_database(
    guard_database: tuple[str, str, UUID],
) -> Iterator[tuple[tuple[str, str, UUID], str]]:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if url is None:
        pytest.skip("set TEST_RABBITMQ_URL for persisted-pointer consumer tests")
    asyncio.run(_reset_topology(url))
    try:
        yield guard_database, url
    finally:
        asyncio.run(_reset_topology(url))


@pytest.mark.integration
@pytest.mark.parametrize("forged_claim", ["provider", "workspace", "project", "head"])
def test_actual_worker_rejects_forged_gitlab_pointer_before_claim(
    guarded_queue_database: tuple[tuple[str, str, UUID], str],
    forged_claim: str,
) -> None:
    database, rabbit = guarded_queue_database
    database_url, schema, _ = database

    async def scenario() -> None:
        async with asyncio.timeout(45), rig(database) as fixture:
            pointer, origin, _ = await seeded(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET message_published_at=now(), available_at=now() "
                        "WHERE id=:id"
                    ),
                    {"id": pointer.run_id},
                )
            body: dict[str, Any] = {
                "schema": "review.run/v1",
                "message_id": str(pointer.run_id),
                "run_id": str(pointer.run_id),
                "workspace_id": str(fixture.workspace),
                "installation_id": 71,
                "repo": {
                    "id": str(pointer.repository_id),
                    "provider": "gitlab",
                    "external_id": 17,
                    "full_name": "team/project",
                },
                "pr": {
                    "number": 12,
                    "head_sha": "a" * 40,
                    "base_sha": "b" * 40,
                    "base_ref": "main",
                },
                "engine": "fast",
                "rule_version_id": str(pointer.rule_version_id),
                "prompt_version_id": str(pointer.prompt_version_id),
                "trigger": "rerun",
                "attempt": 1,
                "requested_at": "2026-10-19T12:00:00Z",
            }
            if forged_claim == "provider":
                body["repo"]["provider"] = "github"
            elif forged_claim == "workspace":
                body["workspace_id"] = "20000000-0000-0000-0000-000000000001"
            elif forged_claim == "project":
                body["repo"]["external_id"] = 18
            else:
                body["pr"]["head_sha"] = "c" * 40
            settings = WorkerSettings.from_environment(
                {
                    "DATABASE_URL": f"{database_url}?options=-csearch_path%3D{schema}",
                    "RABBITMQ_URL": rabbit,
                    "WORKER_ID": "pointer-guard-proof",
                }
            )
            provider_calls: list[UUID] = []

            def provider(claimed: ClaimedAttempt) -> Model:
                provider_calls.append(claimed.run_id)
                raise AssertionError("forged pointer must not construct a provider")

            worker = asyncio.create_task(
                run_worker(settings, delays=SHORT_DELAYS, provider_factory=provider)
            )
            try:
                async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                    probe = await channels.connection.channel(publisher_confirms=True)
                    try:
                        async with asyncio.timeout(20):
                            while True:
                                if worker.done():
                                    await worker
                                ready = await probe.declare_queue(run_queue("fast"), passive=True)
                                # RobustChannel caches passive declarations; refresh broker counts.
                                await ready.declare()
                                if ready.declaration_result.consumer_count:
                                    break
                                await asyncio.sleep(0.05)
                        exchange = await probe.get_exchange(EXCHANGE, ensure=False)
                        await exchange.publish(
                            aio_pika.Message(
                                json.dumps(body).encode(),
                                message_id=str(pointer.run_id),
                                content_type="application/json",
                                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            ),
                            routing_key=run_queue("fast"),
                        )
                        dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)
                        dead: dict[str, Any] | None = None
                        async with asyncio.timeout(10):
                            while True:
                                if worker.done():
                                    await worker
                                state = await run_state(fixture.sessions, pointer.run_id)
                                message = await dlq.get(fail=False)
                                if message is not None:
                                    dead = json.loads(message.body)
                                    # A miswired UUID handler would retry before dead-lettering.
                                    assert "x-attempt" not in (message.headers or {})
                                    deaths = (message.headers or {})["x-death"]
                                    assert isinstance(deaths, list) and len(deaths) == 1
                                    death = deaths[0]
                                    assert isinstance(death, dict)
                                    assert death["reason"] == "rejected"
                                    assert death["queue"] == "review.run.fast"
                                    await message.ack()
                                if state != ("queued", 0, None) or dead is not None:
                                    break
                                await asyncio.sleep(0.05)
                        # This assertion measures committed mutation in the pre-wiring RED.
                        assert state == ("queued", 0, None)
                        assert dead == body
                        assert provider_calls == []
                        async with fixture.sessions() as session:
                            assert (
                                await session.scalar(
                                    text(
                                        "SELECT origin_provider_installation_id FROM runs "
                                        "WHERE id=:id"
                                    ),
                                    {"id": pointer.run_id},
                                )
                                == origin
                            )
                    finally:
                        await probe.close()
            finally:
                worker.cancel()
                results = await asyncio.wait_for(
                    asyncio.gather(worker, return_exceptions=True),
                    timeout=5,
                )
                assert isinstance(results[0], asyncio.CancelledError)

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("disposition", ["valid", "missing", "retired"])
def test_real_consumer_acks_valid_missing_and_retired_pointers(
    guarded_queue_database: tuple[tuple[str, str, UUID], str],
    provider: str,
    disposition: str,
) -> None:
    database, rabbit = guarded_queue_database

    async def scenario() -> None:
        async with asyncio.timeout(30), rig(database) as fixture:
            pointer, origin, _ = await seeded(fixture, provider)
            run_id = pointer.run_id
            if disposition == "missing":
                run_id = UUID("20000000-0000-0000-0000-000000000002")
            elif disposition == "retired":
                if provider == "gitlab":
                    await fixture.lifecycle.revoke_installation(installation_id=origin)
                else:
                    async with fixture.sessions.begin() as session:
                        await session.execute(
                            text("UPDATE provider_installations SET deleted_at=now() WHERE id=:id"),
                            {"id": origin},
                        )
            pending = StoredRunMessage(
                run_id=run_id,
                workspace_id=fixture.workspace,
                installation_id=71,
                repository_id=pointer.repository_id,
                repository_external_id=17,
                repository_full_name="renamed/project",
                pr_number=12,
                head_sha="a" * 40,
                base_sha="b" * 40,
                base_ref="old-branch",
                engine="fast",
                rule_version_id=pointer.rule_version_id,
                prompt_version_id=pointer.prompt_version_id,
                attempt=99,
                requested_at=datetime(2000, 1, 1, tzinfo=UTC),
                provider=provider,
                trigger="rerun",
            )
            calls: list[UUID] = []

            async def lifecycle(accepted: UUID) -> DeliveryOutcome:
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                calls.append(accepted)
                return DeliveryOutcome.ACK

            handler = HandleRunDelivery(SqlAlchemyRunDeliveryReader(fixture.sessions), lifecycle)
            async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                await channels.publisher.publish_confirmed(pending)
                queue = await channels.consumer_queue(run_queue("fast"))
                try:
                    async with asyncio.timeout(10):
                        while (message := await queue.get(fail=False)) is None:
                            await asyncio.sleep(0.05)
                    await handle_run_delivery(message, handler.execute, channels.publisher)
                    assert message.processed
                    assert calls == ([pointer.run_id] if disposition == "valid" else [])
                    dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)
                    try:
                        assert await dlq.get(fail=False) is None
                    finally:
                        await dlq.channel.close()
                finally:
                    await queue.channel.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_unavailable_sql_reader_retries_twice_then_real_broker_dead_letters(
    guarded_queue_database: tuple[tuple[str, str, UUID], str],
) -> None:
    database, rabbit = guarded_queue_database

    async def scenario() -> None:
        async with asyncio.timeout(30), rig(database) as fixture:
            pointer, _, _ = await seeded(fixture)
            pending = StoredRunMessage(
                run_id=pointer.run_id,
                workspace_id=fixture.workspace,
                installation_id=71,
                repository_id=pointer.repository_id,
                repository_external_id=17,
                repository_full_name="team/project",
                pr_number=12,
                head_sha="a" * 40,
                base_sha="b" * 40,
                base_ref="main",
                engine="fast",
                rule_version_id=pointer.rule_version_id,
                prompt_version_id=pointer.prompt_version_id,
                attempt=1,
                requested_at=datetime(2026, 10, 19, tzinfo=UTC),
                provider="gitlab",
                trigger="rerun",
            )
            calls: list[UUID] = []

            async def lifecycle(run_id: UUID) -> DeliveryOutcome:
                calls.append(run_id)
                return DeliveryOutcome.ACK

            # Bound but not listening: real driver refusal, with no external service.
            with socket.socket() as unavailable:
                unavailable.bind(("127.0.0.1", 0))
                url = make_url(database[0]).set(port=unavailable.getsockname()[1])
                engine = create_async_engine(url, pool_size=1, max_overflow=0)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                handler = HandleRunDelivery(SqlAlchemyRunDeliveryReader(sessions), lifecycle)
                try:
                    async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                        await channels.publisher.publish_confirmed(pending)
                        queue = await channels.consumer_queue(run_queue("fast"))
                        dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)
                        try:
                            attempts: list[object] = []
                            for _ in range(3):
                                async with asyncio.timeout(10):
                                    while (message := await queue.get(fail=False)) is None:
                                        await asyncio.sleep(0.05)
                                attempts.append((message.headers or {}).get("x-attempt", 0))
                                await handle_run_delivery(
                                    message, handler.execute, channels.publisher
                                )
                                assert message.processed
                            assert attempts == [0, 1, 2]
                            assert calls == []
                            async with asyncio.timeout(10):
                                while (dead := await dlq.get(fail=False)) is None:
                                    await asyncio.sleep(0.05)
                            assert (dead.headers or {}).get("x-attempt") == 2
                            assert json.loads(dead.body)["run_id"] == str(pointer.run_id)
                            await dead.ack()
                            assert await queue.get(fail=False) is None
                            assert await run_state(fixture.sessions, pointer.run_id) == (
                                "queued",
                                0,
                                None,
                            )
                        finally:
                            await queue.channel.close()
                            await dlq.channel.close()
                finally:
                    await engine.dispose()

    asyncio.run(scenario())
