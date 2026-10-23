"""Persisted Run-origin admission on real PostgreSQL and bounded pool1."""

from __future__ import annotations

import asyncio
import socket
import traceback
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import visitors

from app.modules.integrations.gitlab.application.request_credentials import (
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
)
from app.modules.reviews.application.run_delivery import (
    HandleRunDelivery,
    RunDeliveryPointer,
    RunDeliveryUnavailable,
)
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.infrastructure.run_delivery import SqlAlchemyRunDeliveryReader
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleUnitOfWork
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import Rig, rig, seed_run
from tests.test_gitlab_rerun_postgres import seed_change, seed_github

run_delivery_database = projection.authority_database
pytestmark = pytest.mark.integration
RULE = UUID("10000000-0000-0000-0000-000000000005")
PROMPT = UUID("10000000-0000-0000-0000-000000000006")


async def bounded(operation: Coroutine[Any, Any, None]) -> None:
    async with asyncio.timeout(30):
        await operation


async def seeded(fixture: Rig, provider: str = "gitlab") -> tuple[RunDeliveryPointer, UUID, UUID]:
    target = await fixture.seed()
    change = await seed_change(fixture, target.repository_id)
    run = await seed_run(fixture, target, change)
    origin, repository = target.origin_provider_installation_id, target.repository_id
    if provider == "github":
        origin, repository = await seed_github(fixture, repository, fixture.workspace, 71)
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE provider_installations SET external_id=71 WHERE id=:id"), {"id": origin}
        )
        await session.execute(
            text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
            {"repo": repository, "id": change},
        )
        await session.execute(
            text(
                "INSERT INTO rule_versions (id,repository_id,version,rules,checksum,is_active) "
                "VALUES (:id,:repo,99,'[]',:checksum,false)"
            ),
            {"id": RULE, "repo": repository, "checksum": "e" * 64},
        )
        await session.execute(
            text(
                "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                "VALUES (:id,'delivery.test',99,'synthetic',:checksum,false)"
            ),
            {"id": PROMPT, "checksum": "e" * 64},
        )
        await session.execute(
            text(
                "UPDATE runs SET origin_provider_installation_id=:origin,rule_version_id=:rule,"
                "prompt_version_id=:prompt,trigger='rerun' WHERE id=:id"
            ),
            {"origin": origin, "rule": RULE, "prompt": PROMPT, "id": run},
        )
    return (
        RunDeliveryPointer(
            run,
            provider,
            fixture.workspace,
            repository,
            17,
            71,
            12,
            "a" * 40,
            "b" * 40,
            "fast",
            RULE,
            PROMPT,
            "rerun",
        ),
        origin,
        change,
    )


class RecordingHandler:
    def __init__(self) -> None:
        self.runs: list[UUID] = []

    async def __call__(self, run_id: UUID) -> DeliveryOutcome:
        self.runs.append(run_id)
        return DeliveryOutcome.ACK


def test_public_reader_and_guard_refuse_rebound_historical_origin(
    run_delivery_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, change = await seeded(fixture)
            # Explicit adversarial historical reparenting, not supported management.
            _, replacement = await seed_github(
                fixture, pointer.repository_id, fixture.workspace, 71
            )
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                    {"repo": replacement, "id": change},
                )
            reader = SqlAlchemyRunDeliveryReader(fixture.sessions)
            handler = RecordingHandler()
            assert await reader.read(pointer.run_id) is None
            assert await HandleRunDelivery(reader, handler).execute(pointer) is DeliveryOutcome.ACK
            assert handler.runs == []
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT origin_provider_installation_id FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                    == origin
                )

    asyncio.run(bounded(scenario()))


class NoRetry:
    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        raise AssertionError("no retry expected")


@pytest.mark.parametrize("provider", ["gitlab", "github"])
@pytest.mark.parametrize("state", ["queued", "cancelled"])
def test_paused_repository_still_reaches_public_lifecycle_without_pipeline(
    run_delivery_database: tuple[str, str, UUID],
    provider: str,
    state: str,
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, change = await seeded(fixture, provider)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_changes SET head_sha=:head WHERE id=:id"),
                    {"head": "a" * 40, "id": change},
                )
                await session.execute(
                    text("UPDATE repositories SET enabled=false WHERE id=:id"),
                    {"id": pointer.repository_id},
                )
                if state == "cancelled":
                    await session.execute(
                        text("UPDATE runs SET state='cancelled',attempt=1 WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
            # Existing default metadata consumers retain strict business readiness.
            if provider == "gitlab":
                with pytest.raises(GitLabTargetInactive):
                    await fixture.reader.resolve_target(GitLabTargetIdentity(origin, 17))
                async with SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions) as uow:
                    assert await uow.runs.run_message(pointer.run_id) is None
            calls: list[UUID] = []
            attempts: list[ClaimedAttempt] = []

            async def pipeline(attempt: ClaimedAttempt) -> bool:
                attempts.append(attempt)
                return True

            lifecycle = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                pipeline=pipeline,
                retry_queue=NoRetry(),
                check_runs=None,
                worker_id="delivery-test",
                now=lambda: projection.NOW,
            )

            async def handler(run_id: UUID) -> DeliveryOutcome:
                calls.append(run_id)
                return await lifecycle.execute(run_id)

            result = await HandleRunDelivery(
                SqlAlchemyRunDeliveryReader(fixture.sessions),
                handler,
            ).execute(pointer)
            assert result is DeliveryOutcome.ACK
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text("SELECT state,error_code,attempt FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                ).one()
            if state == "queued":
                assert tuple(row) == ("skipped", "repo_disabled", 0)
            else:
                assert tuple(row) == ("cancelled", None, 1)
            assert calls == [pointer.run_id]
            assert attempts == []

    asyncio.run(bounded(scenario()))


def test_actual_database_connection_failure_is_safe_and_distinct_from_missing() -> None:
    async def scenario() -> None:
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        url = URL.create(
            "postgresql+psycopg",
            username="dsn-secret-canary",
            password="secret-canary",
            host="127.0.0.1",
            port=port,
            database="unavailable",
        )
        engine = create_async_engine(url, connect_args={"connect_timeout": 1})
        try:
            reader = SqlAlchemyRunDeliveryReader(async_sessionmaker(engine))
            with pytest.raises(RunDeliveryUnavailable) as caught:
                await asyncio.wait_for(reader.read(UUID(int=1)), 3)
            assert str(caught.value) == "Run delivery identity unavailable"
            assert caught.value.__cause__ is None
            rendered = "".join(traceback.format_exception(caught.value))
            assert "secret-canary" not in rendered
            assert "SELECT" not in rendered
        finally:
            await engine.dispose()

    asyncio.run(bounded(scenario()))


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_pinned_old_run_metadata_is_detached_before_handler_on_same_pool1(
    run_delivery_database: tuple[str, str, UUID],
    provider: str,
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, change = await seeded(fixture, provider)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE code_changes SET state='closed',target_branch='renamed' "
                        "WHERE id=:id"
                    ),
                    {"id": change},
                )
                await session.execute(
                    text("UPDATE repositories SET full_name='renamed/project' WHERE id=:id"),
                    {"id": pointer.repository_id},
                )
                await session.execute(
                    text("UPDATE runs SET attempt=2,base_ref=NULL,created_at=:old WHERE id=:id"),
                    {"old": projection.NOW - timedelta(days=1), "id": pointer.run_id},
                )
            projections: list[tuple[str, ...]] = []

            def observed(connection: Any, statement: Any, *args: Any) -> None:
                for node in visitors.iterate(statement):
                    if isinstance(node, Select):
                        projections.append(tuple(str(column) for column in node.selected_columns))

            event.listen(fixture.engine.sync_engine, "before_execute", observed)
            reader = SqlAlchemyRunDeliveryReader(fixture.sessions)
            identity = await reader.read(pointer.run_id)
            assert identity is not None
            assert identity.pointer == pointer
            assert identity.origin_provider_installation_id == origin
            event.remove(fixture.engine.sync_engine, "before_execute", observed)
            assert projections
            for columns in projections:
                for secret in (
                    "token_ciphertext",
                    "credential_key_id",
                    "webhook_secret_digest",
                    "metadata",
                ):
                    assert all(secret not in column for column in columns)
            calls: list[UUID] = []

            async def handler(run_id: UUID) -> DeliveryOutcome:
                # Public SAME pool1 read succeeds only after delivery reader has closed.
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT head_sha FROM runs WHERE id=:id"), {"id": run_id}
                        )
                        == "a" * 40
                    )
                observer = create_async_engine(
                    run_delivery_database[0],
                    poolclass=NullPool,
                    isolation_level="AUTOCOMMIT",
                    hide_parameters=True,
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
                calls.append(run_id)
                return DeliveryOutcome.ACK

            assert (
                await asyncio.wait_for(
                    HandleRunDelivery(reader, handler).execute(pointer),
                    5,
                )
                is DeliveryOutcome.ACK
            )
            assert calls == [pointer.run_id]

    asyncio.run(bounded(scenario()))


@pytest.mark.parametrize("provider", ["gitlab", "github"])
@pytest.mark.parametrize("state", ["absent", "null", "deleted", "unavailable"])
def test_absent_or_retired_origin_is_acknowledged_without_handler(
    run_delivery_database: tuple[str, str, UUID],
    provider: str,
    state: str,
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, _ = await seeded(fixture, provider)
            statements = {
                "absent": "DELETE FROM runs WHERE id=:run",
                "null": "UPDATE runs SET origin_provider_installation_id=NULL WHERE id=:run",
                "deleted": "UPDATE provider_installations SET deleted_at=now() WHERE id=:origin",
                "unavailable": (
                    "UPDATE repositories SET installation_available=false WHERE id=:repo"
                ),
            }
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(statements[state]),
                    {"run": pointer.run_id, "origin": origin, "repo": pointer.repository_id},
                )
            handler = RecordingHandler()
            reader = SqlAlchemyRunDeliveryReader(fixture.sessions)
            assert await reader.read(pointer.run_id) is None
            assert await HandleRunDelivery(reader, handler).execute(pointer) is DeliveryOutcome.ACK
            assert handler.runs == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT 1")) == 1

    asyncio.run(bounded(scenario()))


@pytest.mark.parametrize("state", ["revoked", "missing_pair", "intake_off", "unbound"])
def test_gitlab_unready_authority_never_reaches_handler(
    run_delivery_database: tuple[str, str, UUID],
    state: str,
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, _ = await seeded(fixture)
            statements = {
                "revoked": "UPDATE gitlab_connections SET revoked_at=now(),intake_enabled=false "
                "WHERE provider_installation_id=:origin",
                "missing_pair": "UPDATE gitlab_connections SET revoked_at=now(),"
                "intake_enabled=false,"
                "token_ciphertext=NULL,credential_key_id=NULL "
                "WHERE provider_installation_id=:origin",
                "intake_off": "UPDATE gitlab_connections SET intake_enabled=false "
                "WHERE provider_installation_id=:origin",
                "unbound": "DELETE FROM gitlab_project_bindings WHERE repository_id=:repo",
            }
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(statements[state]), {"origin": origin, "repo": pointer.repository_id}
                )
            handler = RecordingHandler()
            assert (
                await HandleRunDelivery(
                    SqlAlchemyRunDeliveryReader(fixture.sessions),
                    handler,
                ).execute(pointer)
                is DeliveryOutcome.ACK
            )
            assert handler.runs == []

    asyncio.run(bounded(scenario()))


@pytest.mark.parametrize("original_provider", ["gitlab", "github"])
@pytest.mark.parametrize("replacement_provider", ["gitlab", "github"])
@pytest.mark.parametrize("other_workspace", [False, True])
def test_colliding_replacement_provider_and_tenant_cannot_adopt_historical_run(
    run_delivery_database: tuple[str, str, UUID],
    original_provider: str,
    replacement_provider: str,
    other_workspace: bool,
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, change = await seeded(fixture, original_provider)
            workspace = uuid4() if other_workspace else fixture.workspace
            if other_workspace:
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO workspaces (id,name,daily_budget_usd) "
                            "VALUES (:id,'other',7)"
                        ),
                        {"id": workspace},
                    )
            if replacement_provider == "gitlab":
                replacement = await fixture.seed(
                    origin="https://other.gitlab.example", workspace=workspace
                )
                replacement_repository = replacement.repository_id
            else:
                _, replacement_repository = await seed_github(
                    fixture,
                    pointer.repository_id,
                    workspace,
                    71 if original_provider == "gitlab" else 72,
                )
            # Persisted project17/MR12 numbers collide, but immutable parent stays original.
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                    {"repo": replacement_repository, "id": change},
                )
            handler = RecordingHandler()
            assert (
                await HandleRunDelivery(
                    SqlAlchemyRunDeliveryReader(fixture.sessions),
                    handler,
                ).execute(pointer)
                is DeliveryOutcome.ACK
            )
            assert handler.runs == []
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT origin_provider_installation_id FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                    == origin
                )

    asyncio.run(bounded(scenario()))


def test_actual_same_origin_rotation_keeps_terminal_old_head_cancellation_admissible(
    run_delivery_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, change = await seeded(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET attempt=1 WHERE id=:id"), {"id": pointer.run_id}
                )
                await session.execute(
                    text("UPDATE code_changes SET state='closed' WHERE id=:id"), {"id": change}
                )
            await fixture.lifecycle.rotate_token(
                installation_id=origin, token="replacement-authority-token"
            )
            async with SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions) as uow:
                outgoing = await uow.runs.run_message(pointer.run_id)
            assert outgoing is not None
            assert (outgoing.attempt, outgoing.head_sha, outgoing.base_ref) == (2, "a" * 40, "main")
            calls: list[UUID] = []
            attempts: list[ClaimedAttempt] = []

            async def pipeline(attempt: ClaimedAttempt) -> bool:
                attempts.append(attempt)
                return True

            lifecycle = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                pipeline=pipeline,
                retry_queue=NoRetry(),
                check_runs=None,
                worker_id="rotation-delivery",
                now=lambda: projection.NOW,
            )

            async def handler(run_id: UUID) -> DeliveryOutcome:
                calls.append(run_id)
                return await lifecycle.execute(run_id)

            assert (
                await HandleRunDelivery(
                    SqlAlchemyRunDeliveryReader(fixture.sessions),
                    handler,
                ).execute(pointer)
                is DeliveryOutcome.ACK
            )
            assert calls == [pointer.run_id]
            assert attempts == []
            async with fixture.sessions() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT state,error_code,origin_provider_installation_id "
                                "FROM runs WHERE id=:id"
                            ),
                            {"id": pointer.run_id},
                        )
                    ).one()
                ) == ("cancelled", "credential_rotated", origin)

    asyncio.run(bounded(scenario()))


def test_committed_retirement_after_detached_guard_still_cannot_claim_replacement(
    run_delivery_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, change = await seeded(fixture)
            calls: list[UUID] = []
            attempts: list[ClaimedAttempt] = []

            async def pipeline(attempt: ClaimedAttempt) -> bool:
                attempts.append(attempt)
                return True

            lifecycle = HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                pipeline=pipeline,
                retry_queue=NoRetry(),
                check_runs=None,
                worker_id="delivery-handoff",
                now=lambda: projection.NOW,
            )

            async def handler(run_id: UUID) -> DeliveryOutcome:
                calls.append(run_id)
                # Real committed retirement BETWEEN admission snapshot and actual claim.
                await fixture.lifecycle.revoke_installation(installation_id=origin)
                replacement = await fixture.seed(origin="https://other.gitlab.example")
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                        {"repo": replacement.repository_id, "id": change},
                    )
                return await lifecycle.execute(run_id)

            assert (
                await asyncio.wait_for(
                    HandleRunDelivery(
                        SqlAlchemyRunDeliveryReader(fixture.sessions),
                        handler,
                    ).execute(pointer),
                    10,
                )
                is DeliveryOutcome.ACK
            )
            assert calls == [pointer.run_id]
            assert attempts == []
            async with fixture.sessions() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT state,origin_provider_installation_id,attempt "
                                "FROM runs WHERE id=:id"
                            ),
                            {"id": pointer.run_id},
                        )
                    ).one()
                ) == ("cancelled", origin, 0)
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1

    asyncio.run(bounded(scenario()))


@pytest.mark.parametrize("fault", ["execute", "close", "cancel", "programmer"])
def test_owned_session_closes_on_fault_without_hiding_cancellation_or_programmer_errors(
    run_delivery_database: tuple[str, str, UUID],
    fault: str,
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, _, _ = await seeded(fixture)
            closes: list[str] = []
            failure: BaseException
            if fault == "cancel":
                failure = asyncio.CancelledError()
            elif fault == "programmer":
                failure = ValueError("mapper-programmer-canary")
            else:
                failure = OperationalError("SELECT secret-sql-canary", {}, Exception("dsn-canary"))

            class BoundarySession(AsyncSession):
                async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                    result = await super().execute(statement, *args, **kwargs)
                    if fault != "close":
                        raise failure
                    return result

                async def close(self) -> None:
                    await super().close()
                    closes.append("closed")
                    if fault == "close":
                        raise failure

            sessions = async_sessionmaker[AsyncSession](fixture.engine, class_=BoundarySession)
            reader = SqlAlchemyRunDeliveryReader(sessions)
            if fault in {"cancel", "programmer"}:
                with pytest.raises(type(failure)) as caught:
                    await reader.read(pointer.run_id)
                assert caught.value is failure
            else:
                with pytest.raises(RunDeliveryUnavailable) as unavailable:
                    await reader.read(pointer.run_id)
                assert str(unavailable.value) == "Run delivery identity unavailable"
                rendered = "".join(traceback.format_exception(unavailable.value))
                assert "secret-sql-canary" not in rendered
                assert "dsn-canary" not in rendered
            assert closes == ["closed"]
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT 1")) == 1

    asyncio.run(bounded(scenario()))


def test_unsupported_persisted_provider_never_falls_back_to_github(
    run_delivery_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(run_delivery_database) as fixture:
            pointer, origin, _ = await seeded(fixture, "github")
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET provider='unsupported' WHERE id=:id"),
                    {"id": origin},
                )
            handler = RecordingHandler()
            assert (
                await HandleRunDelivery(
                    SqlAlchemyRunDeliveryReader(fixture.sessions),
                    handler,
                ).execute(pointer)
                is DeliveryOutcome.ACK
            )
            assert handler.runs == []

    asyncio.run(bounded(scenario()))
