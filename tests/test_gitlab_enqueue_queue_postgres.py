"""Actual PG/Rabbit initial and outbox confirmations, without a review worker."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractChannel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.reviews.application.project_gitlab_merge_request import (
    GitLabMergeRequestCommand,
    HandleGitLabMergeRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunMessagePublisher,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher, validate_message
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionLock,
)
from tests import test_gitlab_projection_postgres as projection

queue_database = projection.authority_database
pytestmark = pytest.mark.integration
_QUEUES = (
    "reviews.dlq",
    "review.publish",
    "review.run.fast",
    "review.run.deep",
    "retry.30s.fast",
    "retry.2m.fast",
    "retry.10m.fast",
    "retry.30s.deep",
    "retry.2m.deep",
    "retry.10m.deep",
)
_EXCHANGES = ("reviews", "reviews.retry", "reviews.dlx")


@pytest.fixture
def broker_url() -> str:
    ci = os.environ.get("GITHUB_ACTIONS") == "true"
    value = os.environ.get("TEST_RABBITMQ_URL") or (os.environ.get("RABBITMQ_URL") if ci else None)
    if value is None:
        pytest.skip("set TEST_RABBITMQ_URL to the disposable exclusive RabbitMQ vhost")
    parsed = urlsplit(value)
    # CI owns an isolated service, matching the established queue test fixtures.
    # Outside CI only the explicitly reserved local namespace is valid.
    if not ci and (parsed.hostname, parsed.port, parsed.path) != (
        "127.0.0.1",
        58447,
        "/dmc169-test",
    ):
        pytest.fail("T9e requires its reserved disposable RabbitMQ namespace")
    return value


async def remove_review_topology(channel: AbstractChannel) -> None:
    for name in _QUEUES:
        await asyncio.wait_for(channel.queue_delete(name), 5)
    for name in _EXCHANGES:
        await asyncio.wait_for(channel.exchange_delete(name), 5)


@asynccontextmanager
async def broker_resources(url: str) -> AsyncIterator[AbstractChannel]:
    connection = await asyncio.wait_for(aio_pika.connect(url, timeout=3), 5)
    try:
        channel = await asyncio.wait_for(connection.channel(), 5)
        await remove_review_topology(channel)
        try:
            yield channel
        finally:
            await remove_review_topology(channel)
    finally:
        await asyncio.wait_for(connection.close(), 5)


async def close_publisher(publisher: LazyAmqpPublisher) -> None:
    await asyncio.wait_for(publisher.aclose(), 5)


@dataclass
class ObservedPublisher:
    fixture: projection.Rig
    database_url: str
    lock: SqlAlchemyGitLabMergeRequestProjectionLock
    command: GitLabMergeRequestCommand
    delegate: RunMessagePublisher
    confirmed: list[PendingRunMessage]
    crash_after_confirm: bool = False

    async def publish_confirmed(
        self,
        message: PendingRunMessage,
        *,
        kind: RunPublicationKind = RunPublicationKind.QUEUED,
    ) -> None:
        # Pool1 is reusable and the original Run is committed before publication.
        async with self.fixture.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.id,r.origin_provider_installation_id,pi.provider,pi.workspace_id,"
                        "r.head_sha,r.base_sha,r.message_published_at FROM runs r JOIN "
                        "provider_installations pi ON pi.id=r.origin_provider_installation_id "
                        "WHERE r.id=:id"
                    ),
                    {"id": message.run_id},
                )
            ).one()
            assert tuple(row) == (
                message.run_id,
                self.command.origin_provider_installation_id,
                "gitlab",
                self.fixture.workspace,
                "a" * 40,
                "b" * 40,
                None,
            )
        observer = create_async_engine(self.database_url, poolclass=NullPool)
        try:
            async with observer.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND backend_type='client backend' AND pid<>pg_backend_pid() "
                            "AND xact_start IS NOT NULL"
                        )
                    )
                    == 0
                )
        finally:
            await observer.dispose()
        # The exact public identity is reacquired and released BEFORE broker I/O.
        async with self.lock.hold(self.command):
            pass
        await asyncio.wait_for(self.delegate.publish_confirmed(message, kind=kind), 5)
        self.confirmed.append(message)
        if self.crash_after_confirm:
            raise asyncio.CancelledError()


async def receive_body(channel: AbstractChannel) -> tuple[dict[str, Any], int, int]:
    queue = await channel.get_queue("review.run.fast", ensure=False)
    message = await asyncio.wait_for(queue.get(timeout=3), 5)
    assert message is not None
    body: dict[str, Any] = json.loads(message.body)
    validate_message("review.run.v1", body)
    assert message.message_id == body["run_id"]
    assert message.content_type == "application/json"
    priority, delivery = message.priority or 0, int(message.delivery_mode or 0)
    await asyncio.wait_for(message.ack(), 5)
    return body, priority, delivery


@pytest.mark.parametrize("mode", ["initial", "unreachable_then_recover", "confirm_then_crash"])
def test_canonical_receipt_real_broker_confirmation_and_same_run_outbox_recovery(
    queue_database: tuple[str, str, UUID],
    broker_url: str,
    mode: str,
    tmp_path: Path,
) -> None:
    failed_connections: list[str] = []

    async def scenario() -> None:
        async with AsyncExitStack() as stack:
            fixture = await stack.enter_async_context(projection.rig(queue_database))
            lock = await stack.enter_async_context(projection.lock_for(queue_database))
            channel = await stack.enter_async_context(broker_resources(broker_url))
            target, clock = await fixture.seed(), projection.Clock()
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: projection.automatic_response(
                            request, projection.canonical(target)
                        )
                    )
                )
            )
            actual = LazyAmqpPublisher(broker_url)
            stack.push_async_callback(close_publisher, actual)
            delegate: RunMessagePublisher = actual
            if mode == "unreachable_then_recover":
                # Owned nonlistening socket: actual unavailable transport, no service.
                refusal_socket = stack.enter_context(socket.socket())
                refusal_socket.bind(("127.0.0.1", 0))
                port = refusal_socket.getsockname()[1]
                failing = LazyAmqpPublisher(
                    f"amqp://synthetic:synthetic@127.0.0.1:{port}/unreachable?timeout=1"
                )
                stack.push_async_callback(close_publisher, failing)

                class BoundedFailure:
                    async def publish_confirmed(
                        self,
                        message: PendingRunMessage,
                        *,
                        kind: RunPublicationKind = RunPublicationKind.QUEUED,
                    ) -> None:
                        try:
                            await asyncio.wait_for(failing.publish_confirmed(message, kind=kind), 3)
                        except (TimeoutError, ConnectionError) as error:
                            failed_connections.append(type(error).__name__)
                            raise

                delegate = BoundedFailure()
            confirmed: list[PendingRunMessage] = []
            observed = ObservedPublisher(
                fixture,
                queue_database[0],
                lock,
                projection.command_for(target),
                delegate,
                confirmed,
                mode == "confirm_then_crash",
            )
            handler = HandleGitLabMergeRequest(
                projector=projection.project(fixture, client, lock),
                enqueuer=projection.automatic_enqueuer(fixture, observed),
            )
            receiver = projection.receiver(fixture, handler, clock)
            await receiver.execute(projection.receipt_for(target))
            assert confirmed == []
            if mode == "confirm_then_crash":
                with pytest.raises(asyncio.CancelledError):
                    await receiver.replay_pending()
            else:
                assert await receiver.replay_pending() == 1
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT r.id,r.message_published_at,pi.external_id,r.rule_version_id,"
                            "r.prompt_version_id FROM runs r JOIN provider_installations pi ON "
                            "pi.id=r.origin_provider_installation_id"
                        )
                    )
                ).one()
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert (row.message_published_at is not None) is (mode == "initial")
                if mode == "unreachable_then_recover":
                    assert (
                        await session.scalar(
                            text("SELECT projected_at IS NOT NULL FROM webhook_events")
                        )
                        is True
                    )
            if mode == "unreachable_then_recover":
                assert len(failed_connections) == 1
            first: dict[str, Any] | None = None
            if mode != "unreachable_then_recover":
                first, priority, delivery = await receive_body(channel)
                assert (priority, delivery) == (0, 2)
            if mode != "initial":
                recovery = ObservedPublisher(
                    fixture,
                    queue_database[0],
                    lock,
                    projection.command_for(target),
                    actual,
                    confirmed,
                )
                assert (
                    await projection.automatic_enqueuer(
                        fixture, recovery
                    ).replay_pending_publications()
                    == 1
                )
                replay, priority, delivery = await receive_body(channel)
                assert (priority, delivery) == (0, 2)
                if first is not None:
                    assert replay == first
                first = replay
            assert first is not None
            assert first["schema"] == "review.run/v1"
            assert first["run_id"] == first["message_id"] == str(row.id)
            assert first["workspace_id"] == str(target.workspace_id)
            assert first["installation_id"] == row.external_id
            assert type(row.external_id) is int and row.external_id > 0
            assert first["repo"] == {
                "id": str(target.repository_id),
                "provider": "gitlab",
                "external_id": 17,
                "full_name": "team/project",
            }
            assert first["pr"] == {
                "number": 12,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "base_ref": "main",
            }
            assert (first["trigger"], first["attempt"], first["engine"]) == ("webhook", 1, "fast")
            assert first["requested_at"] == "2026-10-19T12:00:00Z"
            assert first["rule_version_id"] == str(row.rule_version_id)
            assert first["prompt_version_id"] == str(row.prompt_version_id)
            assert set(first) == {
                "schema",
                "message_id",
                "run_id",
                "workspace_id",
                "installation_id",
                "repo",
                "pr",
                "engine",
                "rule_version_id",
                "prompt_version_id",
                "trigger",
                "attempt",
                "requested_at",
            }
            if mode == "confirm_then_crash":
                clock.value += timedelta(minutes=6)
                assert await projection.receiver(fixture, handler, clock).replay_pending() == 1
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert (
                    await session.scalar(text("SELECT message_published_at IS NOT NULL FROM runs"))
                    is True
                )
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 1
            assert len(confirmed) == (2 if mode == "confirm_then_crash" else 1)
            if mode == "initial":
                # Actual GitHub wire positive control, explicitly a synthetic pointer.
                github = replace(confirmed[0], run_id=uuid4(), provider="github")
                await asyncio.wait_for(actual.publish_confirmed(github), 5)
                gh_body, priority, delivery = await receive_body(channel)
                assert gh_body["repo"]["provider"] == "github"
                assert gh_body["run_id"] == str(github.run_id)
                assert (priority, delivery) == (0, 2)
            queue = await channel.get_queue("review.run.fast", ensure=False)
            assert await queue.get(fail=False, timeout=1) is None

    asyncio.run(asyncio.wait_for(scenario(), 30))
    (tmp_path / "transport-outcome.json").write_text(
        json.dumps({"mode": mode, "unavailable_error_types": failed_connections}, indent=2) + "\n"
    )
