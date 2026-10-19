"""Public review-worker leader replay on PostgreSQL; recording queue, no broker."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractExchange
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.integrations.gitlab.application.request_credentials import BoundGitLabTarget
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.amqp import AmqpQueuePublisher
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
)
from app.worker import WorkerProcess, WorkerSettings, compose_worker_process
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import NOW, Rig, rig, seed_run
from tests.test_gitlab_rerun_postgres import seed_change, seed_github

worker_outbox_database = projection.authority_database
pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class SeededRun:
    target: BoundGitLabTarget
    run_id: UUID
    change_id: UUID
    origin_id: UUID
    repository_id: UUID
    rule_id: UUID
    prompt_id: UUID
    provider: str
    installation_id: int


async def pending_run(fixture: Rig, *, provider: str = "gitlab", attempt: int = 0) -> SeededRun:
    target = await fixture.seed()
    origin, repository, installation = (
        target.origin_provider_installation_id,
        target.repository_id,
        1,
    )
    if provider == "github":
        origin, repository = await seed_github(fixture, repository, fixture.workspace, 71)
        installation = 71
    change = await seed_change(fixture, repository)
    run = await seed_run(fixture, target, change, origin=origin, attempt=attempt)
    async with fixture.sessions.begin() as session:
        rule = await session.scalar(
            text("SELECT id FROM rule_versions WHERE repository_id=:id AND is_active"),
            {"id": repository},
        )
        prompt = await session.scalar(text("SELECT id FROM prompt_versions WHERE is_active"))
        assert isinstance(rule, UUID) and isinstance(prompt, UUID)
        await session.execute(
            text(
                "UPDATE runs SET trigger='webhook',base_ref='main',created_at=:now,"
                "rule_version_id=:rule WHERE id=:id"
            ),
            {"id": run, "now": NOW, "rule": rule},
        )
        await session.execute(
            text(
                "UPDATE code_changes SET head_sha=repeat('a',40),ai_review_labeled=true "
                "WHERE id=:id"
            ),
            {"id": change},
        )
    return SeededRun(target, run, change, origin, repository, rule, prompt, provider, installation)


class RecordingExchange:
    """External confirming port: intentionally performs no RabbitMQ I/O."""

    def __init__(self) -> None:
        self.reject = True
        self.attempts: list[tuple[dict[str, Any], int | None]] = []
        self.confirmed_at: list[datetime] = []

    async def publish(self, message: aio_pika.Message, routing_key: str) -> None:
        assert routing_key == "review.run.fast"
        assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        body = json.loads(message.body)
        assert message.message_id == body["run_id"]
        self.attempts.append((body, message.priority))
        await asyncio.sleep(0)
        if self.reject:
            raise ConnectionError("synthetic recording-port confirmation failure")
        self.confirmed_at.append(datetime.now(UTC))


class ObservedQueue:
    def __init__(self, fixture: Rig, url: str, seeded: SeededRun) -> None:
        self.fixture, self.url, self.seeded = fixture, url, seeded
        self.exchange = RecordingExchange()
        self.kinds: list[RunPublicationKind] = []
        exchange = cast(AbstractExchange, self.exchange)
        self.publisher = AmqpQueuePublisher(exchange, exchange)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        # Reacquiring the SAME pool1 observes committed identity before publication.
        async with self.fixture.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT origin_provider_installation_id,message_published_at,"
                        "cancellation_signal_published_at FROM runs WHERE id=:id"
                    ),
                    {"id": message.run_id},
                )
            ).one()
            assert row[0] == self.seeded.origin_id
            assert row[1 if kind is RunPublicationKind.QUEUED else 2] is None
        # Close that read before an independent AUTOCOMMIT boundary observation.
        observer = create_async_engine(
            self.url, poolclass=NullPool, isolation_level="AUTOCOMMIT", hide_parameters=True
        )
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
        self.kinds.append(kind)
        await self.publisher.publish_confirmed(message, kind=kind)

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        raise AssertionError("leader replay must not execute retry analysis")

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        raise AssertionError("leader replay must not execute review publication")


def process(fixture: Rig, url: str, queue: ObservedQueue) -> WorkerProcess:
    settings = WorkerSettings.from_environment(
        {"DATABASE_URL": url, "RABBITMQ_URL": "amqp://unused-recording-port"}
    )
    composed = compose_worker_process(
        settings=settings, session_factory=fixture.sessions, queue=queue, github=None
    )
    assert composed.sweep is None
    return composed


def expected_body(
    seeded: SeededRun, *, trigger: str = "webhook", attempt: int = 1, requested_at: datetime = NOW
) -> dict[str, Any]:
    return {
        "schema": "review.run/v1",
        "message_id": str(seeded.run_id),
        "run_id": str(seeded.run_id),
        "workspace_id": str(seeded.target.workspace_id),
        "installation_id": seeded.installation_id,
        "repo": {
            "id": str(seeded.repository_id),
            "provider": seeded.provider,
            "external_id": 17,
            "full_name": "team/project",
        },
        "pr": {"number": 12, "head_sha": "a" * 40, "base_sha": "b" * 40, "base_ref": "main"},
        "engine": "fast",
        "rule_version_id": str(seeded.rule_id),
        "prompt_version_id": str(seeded.prompt_id),
        "trigger": trigger,
        "attempt": attempt,
        "requested_at": requested_at.isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_public_worker_without_github_replays_same_run_after_failed_confirmation(
    worker_outbox_database: tuple[str, str, UUID],
    provider: str,
) -> None:
    async def scenario() -> None:
        async with rig(worker_outbox_database) as fixture:
            seeded = await pending_run(fixture, provider=provider)
            queue = ObservedQueue(fixture, worker_outbox_database[0], seeded)
            worker = process(fixture, worker_outbox_database[0], queue)
            await worker.leader_tick()
            async with fixture.sessions() as session:
                assert tuple(
                    (await session.execute(text("SELECT id,message_published_at FROM runs"))).one()
                ) == (seeded.run_id, None)
            queue.exchange.reject = False
            await worker.leader_tick()
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id,origin_provider_installation_id,"
                            "message_published_at,attempt FROM runs"
                        )
                    )
                ).one()
                assert row[0:2] == (seeded.run_id, seeded.origin_id)
                assert row[2] >= queue.exchange.confirmed_at[0]
                assert row[3] == 0
            await worker.leader_tick()
            assert queue.exchange.attempts == [(expected_body(seeded), 0)] * 2
            assert queue.kinds == [RunPublicationKind.QUEUED] * 2

    asyncio.run(asyncio.wait_for(scenario(), 30))


async def rebind_change(fixture: Rig, seeded: SeededRun) -> None:
    other_workspace = uuid4()
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'replacement',7)"),
            {"id": other_workspace},
        )
    replacement = await fixture.seed(
        origin="https://other.gitlab.example", workspace=other_workspace
    )
    # Historical Run stays A while the current CodeChange points to B in another tenant/instance.
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE code_changes SET repository_id=:repo WHERE id=:change"),
            {"repo": replacement.repository_id, "change": seeded.change_id},
        )


@pytest.mark.parametrize("mutation", ["rotate", "revoke", "rebind"])
def test_public_worker_does_not_republish_or_adopt_changed_queued_origin(
    worker_outbox_database: tuple[str, str, UUID], mutation: str
) -> None:
    async def scenario() -> None:
        async with rig(worker_outbox_database) as fixture:
            seeded = await pending_run(fixture)
            queue = ObservedQueue(fixture, worker_outbox_database[0], seeded)
            worker = process(fixture, worker_outbox_database[0], queue)
            await worker.leader_tick()
            if mutation == "rotate":
                await fixture.lifecycle.rotate_token(
                    installation_id=seeded.origin_id,
                    token="replacement-authority-token",
                    expected_generation=1,
                )
            elif mutation == "revoke":
                await fixture.lifecycle.revoke_installation(installation_id=seeded.origin_id)
            else:
                await rebind_change(fixture, seeded)
            queue.exchange.reject = False
            await worker.leader_tick()
            await worker.leader_tick()
            assert queue.exchange.attempts == [(expected_body(seeded), 0)]
            assert queue.exchange.confirmed_at == []
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id,origin_provider_installation_id,state,attempt,"
                            "cancel_requested,error_code,message_published_at,"
                            "cancellation_signal_requested_at FROM runs"
                        )
                    )
                ).one()
                expected_state = "queued" if mutation == "rebind" else "cancelled"
                expected_error = {
                    "rotate": "credential_rotated",
                    "revoke": "installation_revoked",
                    "rebind": None,
                }[mutation]
                assert tuple(row) == (
                    seeded.run_id,
                    seeded.origin_id,
                    expected_state,
                    0,
                    mutation != "rebind",
                    expected_error,
                    None,
                    None,
                )

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_public_worker_keeps_rotated_terminal_rerun_cancellation_without_reviving_work(
    worker_outbox_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(worker_outbox_database) as fixture:
            seeded = await pending_run(fixture, attempt=2)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET trigger='rerun' WHERE id=:id"), {"id": seeded.run_id}
                )
                await session.execute(
                    text(
                        "UPDATE code_changes SET head_sha=repeat('d',40),state='closed' "
                        "WHERE id=:id"
                    ),
                    {"id": seeded.change_id},
                )
            before_rotation = datetime.now(UTC)
            await fixture.lifecycle.rotate_token(
                installation_id=seeded.origin_id,
                token="replacement-authority-token",
                expected_generation=1,
            )
            after_rotation = datetime.now(UTC)
            async with fixture.sessions() as session:
                requested = await session.scalar(
                    text("SELECT cancellation_signal_requested_at FROM runs WHERE id=:id"),
                    {"id": seeded.run_id},
                )
                assert isinstance(requested, datetime)
                assert before_rotation <= requested <= after_rotation
            queue = ObservedQueue(fixture, worker_outbox_database[0], seeded)
            worker = process(fixture, worker_outbox_database[0], queue)
            await worker.leader_tick()
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(text("SELECT cancellation_signal_published_at FROM runs"))
                    is None
                )
            queue.exchange.reject = False
            await worker.leader_tick()
            await worker.leader_tick()
            expected = expected_body(seeded, trigger="rerun", attempt=3, requested_at=requested)
            assert queue.exchange.attempts == [(expected, 9)] * 2
            assert queue.kinds == [RunPublicationKind.CANCELLATION] * 2
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id,origin_provider_installation_id,state,attempt,trigger,"
                            "error_code,message_published_at,cancellation_signal_published_at "
                            "FROM runs"
                        )
                    )
                ).one()
                assert tuple(row[:7]) == (
                    seeded.run_id,
                    seeded.origin_id,
                    "cancelled",
                    2,
                    "rerun",
                    "credential_rotated",
                    None,
                )
                assert row[7] >= queue.exchange.confirmed_at[0]

    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize("mutation", ["revoke", "rebind"])
def test_public_worker_never_adopts_a_replacement_for_terminal_cancellation(
    worker_outbox_database: tuple[str, str, UUID], mutation: str
) -> None:
    async def scenario() -> None:
        async with rig(worker_outbox_database) as fixture:
            seeded = await pending_run(fixture, attempt=2)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET trigger='rerun' WHERE id=:id"), {"id": seeded.run_id}
                )
            async with SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(fixture.sessions) as uow:
                await uow.runs.cancel_for_merge_request(
                    seeded.target, seeded.change_id, "superseded", NOW, current_head="d" * 40
                )
                await uow.commit()
            if mutation == "revoke":
                await fixture.lifecycle.revoke_installation(installation_id=seeded.origin_id)
            else:
                await rebind_change(fixture, seeded)
            queue = ObservedQueue(fixture, worker_outbox_database[0], seeded)
            queue.exchange.reject = False
            worker = process(fixture, worker_outbox_database[0], queue)
            await worker.leader_tick()
            await worker.leader_tick()
            assert queue.exchange.attempts == []
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id,origin_provider_installation_id,state,attempt,trigger,"
                            "cancellation_signal_requested_at,cancellation_signal_published_at,"
                            "message_published_at FROM runs"
                        )
                    )
                ).one()
                assert tuple(row) == (
                    seeded.run_id,
                    seeded.origin_id,
                    "cancelled",
                    2,
                    "rerun",
                    NOW,
                    None,
                    None,
                )

    asyncio.run(asyncio.wait_for(scenario(), 30))
