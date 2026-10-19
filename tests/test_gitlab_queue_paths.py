"""Origin-pinned GitLab lifecycle and cancellation pointers on real PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractExchange
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.reviews.application.publish_cancellation_signals import PublishCancellationSignals
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.amqp import AmqpQueuePublisher, run_message_body
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleUnitOfWork
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import NOW, Rig, rig, seed_run
from tests.test_gitlab_rerun_postgres import seed_change, seed_github

queue_database = projection.authority_database
pytestmark = pytest.mark.integration


async def bounded(operation: Coroutine[Any, Any, None]) -> None:
    async with asyncio.timeout(30):
        await operation


class ObservedPublisher:
    def __init__(self, fixture: Rig, url: str) -> None:
        self.fixture = fixture
        self.url = url
        self.bodies: list[dict[str, Any]] = []

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        # SAME bounded pool: committed read is possible only after producer UOW exits.
        async with self.fixture.sessions() as session:
            row = (
                await session.execute(
                    text("SELECT state,origin_provider_installation_id FROM runs WHERE id=:id"),
                    {"id": message.run_id},
                )
            ).one()
            assert row[1] is not None
        observer = create_async_engine(self.url, poolclass=NullPool, hide_parameters=True)
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
        self.bodies.append(run_message_body(message))

    async def publish_review(self, pointer: object) -> None:
        raise AssertionError("no review pointer expected in this scenario")


@pytest.mark.parametrize("provider", ["gitlab", "github"])
@pytest.mark.parametrize(
    ("state", "attempt", "expected"),
    [
        ("queued", 1, "queued"),
        ("running", 1, "queued"),
        ("running", 3, "failed"),
    ],
)
def test_public_reconcile_preserves_gitlab_provider_and_stored_rerun_pointer(
    queue_database: tuple[str, str, UUID], state: str, attempt: int, expected: str, provider: str
) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            run = await seed_run(fixture, target, change, state=state, attempt=attempt)
            repository = target.repository_id
            if provider == "github":
                origin, repository = await seed_github(
                    fixture, target.repository_id, fixture.workspace, 71
                )
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                        {"repo": repository, "id": change},
                    )
                    await session.execute(
                        text(
                            "UPDATE runs SET origin_provider_installation_id=:origin WHERE id=:id"
                        ),
                        {"origin": origin, "id": run},
                    )
            created = NOW - timedelta(hours=1)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET trigger='rerun',base_ref='main',created_at=:created,"
                        "available_at=:created,lease_until=:expired WHERE id=:id"
                    ),
                    {"created": created, "expired": NOW - timedelta(seconds=1), "id": run},
                )
            async with fixture.sessions() as session:
                expected_installation, expected_rule, expected_prompt = (
                    await session.execute(
                        text(
                            "SELECT p.external_id,r.rule_version_id,r.prompt_version_id "
                            "FROM runs r "
                            "JOIN provider_installations p "
                            "ON p.id=r.origin_provider_installation_id "
                            "WHERE r.id=:id"
                        ),
                        {"id": run},
                    )
                ).one()
            publisher = ObservedPublisher(fixture, queue_database[0])
            result = await asyncio.wait_for(
                ReconcileRuns(
                    uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                    run_publisher=publisher,
                    review_queue=publisher,
                    now=lambda: NOW,
                ).execute(),
                5,
            )
            assert result == 1
            assert len(publisher.bodies) == 1
            body = publisher.bodies[0]
            assert body["repo"] == {
                "id": str(repository),
                "provider": provider,
                "external_id": 17,
                "full_name": "team/project",
            }
            assert body["run_id"] == body["message_id"] == str(run)
            assert body["workspace_id"] == str(fixture.workspace)
            assert body["installation_id"] == expected_installation
            assert body["rule_version_id"] == str(expected_rule)
            assert body["prompt_version_id"] == str(expected_prompt)
            assert body["engine"] == "fast"
            assert body["pr"] == {
                "number": 12,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "base_ref": "main",
            }
            assert (body["trigger"], body["attempt"], body["requested_at"]) == (
                "rerun",
                attempt + 1,
                created.isoformat().replace("+00:00", "Z"),
            )
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": run})
                    == expected
                )

    asyncio.run(bounded(scenario()))


class RecordingExchange:
    def __init__(self) -> None:
        self.messages: list[aio_pika.Message] = []

    async def publish(self, message: aio_pika.Message, routing_key: str) -> None:
        assert routing_key == "review.run.fast"
        assert message.priority == 9
        self.messages.append(message)


class CancellationPublisher(ObservedPublisher):
    mode = "ok"

    def __init__(self, fixture: Rig, url: str) -> None:
        super().__init__(fixture, url)
        self.exchange = RecordingExchange()

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert kind is RunPublicationKind.CANCELLATION
        async with self.fixture.sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT cancellation_signal_published_at FROM runs WHERE id=:id"),
                    {"id": message.run_id},
                )
                is None
            )
        await super().publish_confirmed(message, kind=kind)
        if self.mode == "fail":
            raise ConnectionError("synthetic unconfirmed publish")
        transport = AmqpQueuePublisher(
            cast(AbstractExchange, self.exchange), cast(AbstractExchange, self.exchange)
        )
        await transport.publish_confirmed(message, kind=kind)
        if self.mode == "crash":
            raise ConfirmedCrash()


class ConfirmedCrash(BaseException):
    pass


def cancellation_publisher(
    fixture: Rig, publisher: CancellationPublisher
) -> PublishCancellationSignals:
    return PublishCancellationSignals(
        uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(fixture.sessions),
        publisher=publisher,
        now=lambda: NOW,
    )


async def cancelled_run(fixture: Rig, *, provider: str = "gitlab", attempt: int = 2) -> UUID:
    target = await fixture.seed()
    change = await seed_change(fixture, target.repository_id)
    run = await seed_run(fixture, target, change, attempt=attempt)
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE runs SET trigger='rerun',base_ref='main' WHERE id=:id"), {"id": run}
        )
    if provider == "github":
        origin, repository = await seed_github(fixture, target.repository_id, fixture.workspace, 71)
        async with fixture.sessions.begin() as session:
            await session.execute(
                text("UPDATE code_changes SET repository_id=:id WHERE id=:cc"),
                {"id": repository, "cc": change},
            )
            await session.execute(
                text("UPDATE runs SET origin_provider_installation_id=:id WHERE id=:run"),
                {"id": origin, "run": run},
            )
        async with SqlAlchemyPullRequestProjectionUnitOfWork(fixture.sessions) as uow:
            await uow.runs.cancel_for_pr(change, "pr_closed", NOW)
            await uow.commit()
    else:
        async with SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(fixture.sessions) as uow:
            await uow.runs.cancel_for_merge_request(
                target, change, "superseded", NOW, current_head="d" * 40
            )
            await uow.commit()
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE code_changes SET state='closed' WHERE id=:id"), {"id": change}
        )
    return run


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_public_cancellation_reconstructs_actual_provider_and_stored_rerun_trigger(
    queue_database: tuple[str, str, UUID],
    provider: str,
) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            run = await cancelled_run(fixture, provider=provider)
            publisher = CancellationPublisher(fixture, queue_database[0])
            result = await asyncio.wait_for(
                cancellation_publisher(fixture, publisher).publish_for((run,)), 5
            )
            assert result == 1
            body = publisher.bodies[0]
            assert body["repo"]["provider"] == provider
            assert body["trigger"] == "rerun"
            assert body["workspace_id"] == str(fixture.workspace)
            assert len(publisher.exchange.messages) == 1
            assert body["run_id"] == str(run)
            assert body["pr"] == {
                "number": 12,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "base_ref": "main",
            }
            assert body["attempt"] == 3
            assert body["requested_at"] == "2026-10-19T12:00:00Z"
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT cancellation_signal_published_at FROM runs WHERE id=:id"),
                        {"id": run},
                    )
                    == NOW
                )

    asyncio.run(bounded(scenario()))


def test_lifecycle_metadata_readiness_is_applied_before_limit(
    queue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            bad = await fixture.seed()
            bad_run = await seed_run(fixture, bad, await seed_change(fixture, bad.repository_id))
            good = await fixture.seed(origin="https://other.gitlab.example")
            good_run = await seed_run(fixture, good, await seed_change(fixture, good.repository_id))
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET intake_enabled=false "
                        "WHERE provider_installation_id=:id"
                    ),
                    {"id": bad.origin_provider_installation_id},
                )
                await session.execute(
                    text("UPDATE runs SET available_at=:old WHERE id=:id"),
                    {"old": NOW - timedelta(hours=2), "id": bad_run},
                )
            async with SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions) as uow:
                assert await uow.runs.run_message(bad_run) is None
                messages = await uow.runs.stale_queued(NOW + timedelta(seconds=1), 1)
            assert [message.run_id for message in messages] == [good_run]

    asyncio.run(bounded(scenario()))


@pytest.mark.parametrize("path", ["lifecycle", "cancellation"])
@pytest.mark.parametrize(
    "condition",
    [
        "intake",
        "unbound",
        "revoked",
        "deleted",
        "unavailable",
        "disabled",
        "null",
        "unsupported",
        "rebound",
    ],
)
def test_ineligible_original_source_cannot_starve_or_adopt_a_healthy_pointer(
    queue_database: tuple[str, str, UUID],
    path: str,
    condition: str,
) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            if path == "cancellation":
                bad_run = await cancelled_run(fixture)
                good_run = await cancelled_run_other_origin(fixture)
            else:
                bad = await fixture.seed()
                bad_run = await seed_run(
                    fixture, bad, await seed_change(fixture, bad.repository_id)
                )
                good = await fixture.seed(origin="https://other.gitlab.example")
                good_run = await seed_run(
                    fixture, good, await seed_change(fixture, good.repository_id)
                )
            async with fixture.sessions() as session:
                origin, repo, change = (
                    await session.execute(
                        text(
                            "SELECT r.origin_provider_installation_id,c.repository_id,c.id "
                            "FROM runs r "
                            "JOIN code_changes c ON c.id=r.code_change_id WHERE r.id=:id"
                        ),
                        {"id": bad_run},
                    )
                ).one()
            if condition == "revoked":
                await fixture.lifecycle.revoke_installation(installation_id=origin)
            elif condition == "rebound":
                # Fixture-labelled historical reparent to another tenant/provider with project17.
                workspace = uuid4()
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO workspaces (id,name,daily_budget_usd) "
                            "VALUES (:id,'other tenant',7)"
                        ),
                        {"id": workspace},
                    )
                _, replacement = await seed_github(fixture, repo, workspace, 71)
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                        {"repo": replacement, "id": change},
                    )
            else:
                statements = {
                    "intake": [
                        "UPDATE gitlab_connections SET intake_enabled=false "
                        "WHERE provider_installation_id=:origin"
                    ],
                    "unbound": ["DELETE FROM gitlab_project_bindings WHERE repository_id=:repo"],
                    "deleted": [
                        "UPDATE provider_installations SET deleted_at=:now WHERE id=:origin"
                    ],
                    "unavailable": [
                        "UPDATE repositories SET installation_available=false WHERE id=:repo"
                    ],
                    "disabled": ["UPDATE repositories SET enabled=false WHERE id=:repo"],
                    "null": ["UPDATE runs SET origin_provider_installation_id=NULL WHERE id=:run"],
                    "unsupported": [
                        "DELETE FROM gitlab_project_bindings WHERE repository_id=:repo",
                        "DELETE FROM gitlab_connections WHERE provider_installation_id=:origin",
                        "UPDATE provider_installations SET provider='bitbucket' WHERE id=:origin",
                    ],
                }[condition]
                async with fixture.sessions.begin() as session:
                    for sql in statements:
                        await session.execute(
                            text(sql), {"origin": origin, "repo": repo, "run": bad_run, "now": NOW}
                        )
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET available_at=:old,"
                        "cancellation_signal_requested_at=CASE WHEN state='cancelled' "
                        "THEN :old ELSE NULL END "
                        "WHERE id=:id"
                    ),
                    {"old": NOW - timedelta(hours=2), "id": bad_run},
                )
            if path == "lifecycle":
                async with SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions) as uow:
                    assert await uow.runs.run_message(bad_run) is None
                    messages = await uow.runs.stale_queued(NOW + timedelta(seconds=1), 1)
                assert [message.run_id for message in messages] == [good_run]
                assert messages[0].provider == "gitlab"
            else:
                publisher = CancellationPublisher(fixture, queue_database[0])
                assert (
                    await asyncio.wait_for(
                        cancellation_publisher(fixture, publisher).replay_pending(limit=1), 5
                    )
                    == 1
                )
                assert [body["run_id"] for body in publisher.bodies] == [str(good_run)]
                assert publisher.bodies[0]["repo"]["provider"] == "gitlab"

    asyncio.run(bounded(scenario()))


async def cancelled_run_other_origin(fixture: Rig) -> UUID:
    target = await fixture.seed(origin="https://other.gitlab.example")
    change = await seed_change(fixture, target.repository_id)
    run = await seed_run(fixture, target, change, attempt=2)
    async with SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(fixture.sessions) as uow:
        await uow.runs.cancel_for_merge_request(
            target, change, "pr_closed", NOW, current_head="d" * 40
        )
        await uow.commit()
    return run


@pytest.mark.parametrize("mode", ["fail", "crash"])
def test_cancellation_confirmation_failure_or_crash_redelivers_the_same_persisted_run(
    queue_database: tuple[str, str, UUID],
    mode: str,
) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            run = await cancelled_run(fixture)
            publisher = CancellationPublisher(fixture, queue_database[0])
            publisher.mode = mode
            use_case = cancellation_publisher(fixture, publisher)
            if mode == "crash":
                with pytest.raises(ConfirmedCrash):
                    await asyncio.wait_for(use_case.publish_for((run,)), 5)
            else:
                assert await asyncio.wait_for(use_case.publish_for((run,)), 5) == 0
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT cancellation_signal_published_at FROM runs WHERE id=:id"),
                        {"id": run},
                    )
                    is None
                )
            publisher.mode = "ok"
            assert await asyncio.wait_for(use_case.replay_pending(), 5) == 1
            assert publisher.bodies[0] == publisher.bodies[1]
            assert len(publisher.exchange.messages) == (2 if mode == "crash" else 1)
            assert await use_case.replay_pending() == 0
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1

    asyncio.run(bounded(scenario()))


def test_same_origin_rotation_keeps_old_head_terminal_cancellation_but_never_requeues(
    queue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            run = await seed_run(fixture, target, change, state="running", attempt=2)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET worker_id='owner',trigger='rerun',"
                        "base_ref='main',lease_until=:now WHERE id=:id"
                    ),
                    {"id": run, "now": NOW},
                )
                await session.execute(
                    text("UPDATE code_changes SET state='closed' WHERE id=:id"), {"id": change}
                )
            await fixture.lifecycle.rotate_token(
                installation_id=target.origin_provider_installation_id,
                token="replacement-authority-token",
            )
            async with SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions) as uow:
                assert not await uow.runs.requeue(run, worker_id="owner", available_at=NOW)
                assert await uow.runs.stale_queued(NOW + timedelta(days=1), 10) == ()
                assert await uow.runs.provider_origin_active(run)
                await uow.commit()
            publisher = CancellationPublisher(fixture, queue_database[0])
            assert (
                await asyncio.wait_for(
                    cancellation_publisher(fixture, publisher).publish_for((run,)), 5
                )
                == 1
            )
            assert publisher.bodies[0]["repo"]["provider"] == "gitlab"
            assert publisher.bodies[0]["trigger"] == "rerun"
            assert publisher.bodies[0]["pr"]["head_sha"] == "a" * 40
            async with fixture.sessions() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT state,error_code,origin_provider_installation_id "
                                "FROM runs WHERE id=:id"
                            ),
                            {"id": run},
                        )
                    ).one()
                ) == ("cancelled", "credential_rotated", target.origin_provider_installation_id)

    asyncio.run(bounded(scenario()))


def test_attempt_zero_cancellation_needs_no_signal(queue_database: tuple[str, str, UUID]) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            run = await cancelled_run(fixture, attempt=0)
            publisher = CancellationPublisher(fixture, queue_database[0])
            assert await cancellation_publisher(fixture, publisher).publish_for((run,)) == 0
            assert publisher.bodies == []

    asyncio.run(bounded(scenario()))


def test_expired_publication_readiness_filters_before_limit_and_keeps_original_pointer(
    queue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(queue_database) as fixture:
            bad = await fixture.seed()
            bad_run = await seed_run(
                fixture,
                bad,
                await seed_change(fixture, bad.repository_id),
                state="publishing",
                attempt=1,
            )
            good = await fixture.seed(origin="https://other.gitlab.example")
            good_run = await seed_run(
                fixture,
                good,
                await seed_change(fixture, good.repository_id),
                state="publishing",
                attempt=1,
            )
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET lease_until=:old"), {"old": NOW - timedelta(hours=1)}
                )
                await session.execute(
                    text("UPDATE runs SET lease_until=:old WHERE id=:id"),
                    {"old": NOW - timedelta(hours=2), "id": bad_run},
                )
                await session.execute(
                    text("DELETE FROM gitlab_project_bindings WHERE repository_id=:id"),
                    {"id": bad.repository_id},
                )
            async with SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions) as uow:
                pointers = await uow.runs.expired_publishing(NOW, 1)
            assert len(pointers) == 1
            assert (pointers[0].run_id, pointers[0].head_sha) == (good_run, "a" * 40)

    asyncio.run(bounded(scenario()))
