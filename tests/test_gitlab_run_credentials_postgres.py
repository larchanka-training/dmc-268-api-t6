"""Real persisted Run/T8 authority, synthetic transport, same pool size one."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.gitlab.infrastructure.run_credentials import GitLabRunCredentials
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import rig, seed_run
from tests.test_gitlab_rerun_postgres import seed_change

run_database = projection.authority_database
pytestmark = pytest.mark.integration


async def checkpoint() -> None:
    pass


def test_rotation_between_requests_refuses_second_transport_call(
    run_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(run_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            run = await seed_run(fixture, target, change)
            expected = await SqlAlchemyRunRepository(
                fixture.sessions, allow_unscoped=True
            ).get_run_vcs_input(run)
            assert expected is not None
            credentials = GitLabRunCredentials(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                run_id=run,
                expected_workspace_id=fixture.workspace,
                expected_input=expected,
                checkpoint=checkpoint,
            )
            calls: list[str] = []

            def external(request: httpx.Request) -> httpx.Response:
                calls.append(request.headers["PRIVATE-TOKEN"])
                return httpx.Response(200)

            async with httpx.AsyncClient(transport=httpx.MockTransport(external)) as client:

                async def request() -> None:
                    credential = await credentials.credential_for_request()
                    await client.get(
                        "https://gitlab.com/api/v4/projects/17",
                        headers={"PRIVATE-TOKEN": credential.token},
                    )

                await request()
                await fixture.lifecycle.rotate_token(
                    installation_id=target.origin_provider_installation_id,
                    token="replacement-authority-token",
                    expected_generation=1,
                )
                with pytest.raises(RunCancelled, match="installation_unavailable"):
                    await request()
            assert calls == ["synthetic-authority-PAT-canary"]

    asyncio.run(asyncio.wait_for(scenario(), 20))


async def seeded_credentials(fixture: projection.Rig) -> tuple[GitLabRunCredentials, UUID]:
    from sqlalchemy import text

    target = await fixture.seed()
    change = await seed_change(fixture, target.repository_id)
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE code_changes SET head_sha=:head WHERE id=:id"),
            {"head": "a" * 40, "id": change},
        )
    run = await seed_run(fixture, target, change)
    expected = await SqlAlchemyRunRepository(
        fixture.sessions, allow_unscoped=True
    ).get_run_vcs_input(run)
    assert expected is not None
    return GitLabRunCredentials(
        session_factory=fixture.sessions,
        authority=fixture.authority,
        run_id=run,
        expected_workspace_id=fixture.workspace,
        expected_input=expected,
        checkpoint=checkpoint,
    ), run


@pytest.mark.parametrize("mutation", ["rotate", "revoke", "rebind", "pause", "head", "start"])
def test_http_every_request_rechecks_real_persisted_authority(
    run_database: tuple[str, str, UUID], mutation: str
) -> None:
    from sqlalchemy import text

    from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
    from app.modules.integrations.gitlab.infrastructure.run_http import (
        GitLabMergeRequestResource,
        GitLabRunHttp,
    )
    from tests.test_gitlab_run_http import Stream

    async def scenario() -> None:
        async with rig(run_database) as fixture:
            credentials, run = await seeded_credentials(fixture)
            target = await credentials.bind()
            calls: list[str] = []

            def external(request: httpx.Request) -> httpx.Response:
                calls.append(request.headers["PRIVATE-TOKEN"])
                return httpx.Response(
                    200, headers={"Content-Type": "application/json"}, stream=Stream((b"{}",))
                )

            async with GitLabRunHttp(
                credentials=credentials,
                origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                transport=httpx.MockTransport(external),
            ) as http:
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
                if mutation == "rotate":
                    await fixture.lifecycle.rotate_token(
                        installation_id=target.origin_provider_installation_id,
                        token="replacement-authority-token",
                        expected_generation=1,
                    )
                elif mutation == "revoke":
                    await fixture.lifecycle.revoke_installation(
                        installation_id=target.origin_provider_installation_id
                    )
                elif mutation == "rebind":
                    other = await fixture.seed(origin="https://other.gitlab.example")
                    async with fixture.sessions.begin() as session:
                        await session.execute(
                            text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                            {
                                "repo": other.repository_id,
                                "id": credentials.expected_input.code_change_id,
                            },
                        )
                else:
                    async with fixture.sessions.begin() as session:
                        if mutation == "pause":
                            await session.execute(
                                text("UPDATE repositories SET enabled=false WHERE id=:id"),
                                {"id": target.repository_id},
                            )
                        elif mutation == "head":
                            await session.execute(
                                text("UPDATE runs SET head_sha=:sha WHERE id=:id"),
                                {"sha": "c" * 40, "id": run},
                            )
                        else:
                            await session.execute(
                                text("UPDATE runs SET base_sha=:sha WHERE id=:id"),
                                {"sha": "c" * 40, "id": run},
                            )
                with pytest.raises(RunCancelled, match="installation_unavailable"):
                    await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
            assert calls == ["synthetic-authority-PAT-canary"]

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_full_expected_input_and_trusted_workspace_are_compared(
    run_database: tuple[str, str, UUID],
) -> None:
    from dataclasses import replace
    from uuid import uuid4

    async def scenario() -> None:
        async with rig(run_database) as fixture:
            original, run = await seeded_credentials(fixture)
            value = original.expected_input
            variants = [
                replace(value, code_change_id=uuid4()),
                replace(value, repository_id=uuid4()),
                replace(value, head_sha="c" * 40),
                replace(value, base_sha="c" * 40),
            ]
            for locator in [
                replace(value.locator, installation_external_id=999),
                replace(value.locator, repository_full_name="other/project"),
                replace(value.locator, number=99),
                replace(value.locator, origin_provider_installation_id=uuid4()),
                replace(value.locator, repository_external_id=18),
            ]:
                variants.append(replace(value, locator=locator))
            for expected in variants:
                credentials = GitLabRunCredentials(
                    session_factory=fixture.sessions,
                    authority=fixture.authority,
                    run_id=run,
                    expected_workspace_id=fixture.workspace,
                    expected_input=expected,
                    checkpoint=checkpoint,
                )
                with pytest.raises(RunCancelled):
                    await credentials.credential_for_request()
            wrong_workspace = GitLabRunCredentials(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                run_id=run,
                expected_workspace_id=uuid4(),
                expected_input=value,
                checkpoint=checkpoint,
            )
            with pytest.raises(RunCancelled):
                await wrong_workspace.credential_for_request()
            with pytest.raises(ValueError):
                GitLabRunCredentials(
                    session_factory=fixture.sessions,
                    authority=fixture.authority,
                    run_id=run,
                    expected_workspace_id=fixture.workspace,
                    expected_input=replace(
                        value, locator=replace(value.locator, provider="github")
                    ),
                    checkpoint=checkpoint,
                )
            assert (
                await original.credential_for_request()
            ).token == "synthetic-authority-PAT-canary"

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_pool_one_reads_commit_before_http_and_independent_observer(
    run_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
    from app.modules.integrations.gitlab.infrastructure.run_http import (
        GitLabMergeRequestResource,
        GitLabRunHttp,
    )
    from tests.test_gitlab_run_http import Stream

    async def scenario() -> None:
        observer = create_async_engine(
            run_database[0], poolclass=NullPool, isolation_level="AUTOCOMMIT", hide_parameters=True
        )
        try:
            async with rig(run_database) as fixture:
                credentials, run = await seeded_credentials(fixture)

                async def external(request: httpx.Request) -> httpx.Response:
                    async with fixture.sessions.begin() as session:
                        assert (
                            await session.scalar(
                                text("SELECT count(*) FROM runs WHERE id=:id"), {"id": run}
                            )
                            == 1
                        )
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
                    return httpx.Response(
                        200, headers={"Content-Type": "application/json"}, stream=Stream((b"{}",))
                    )

                async with GitLabRunHttp(
                    credentials=credentials,
                    origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                    transport=httpx.MockTransport(external),
                ) as http:
                    assert (
                        await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
                    ).data == {}
        finally:
            await observer.dispose()

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("stage", ["first_checkpoint", "final_checkpoint", "persisted_read"])
def test_real_sql_operational_failure_at_each_admission_stage_is_required(
    run_database: tuple[str, str, UUID], stage: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.modules.reviews.application.handle_review_run import AttemptCheckpoint
    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable
    from app.modules.reviews.infrastructure.run_lifecycle_store import (
        SqlAlchemyRunLifecycleUnitOfWork,
    )

    async def scenario() -> None:
        broken = create_async_engine(
            make_url(run_database[0]).set(port=1),
            connect_args={"connect_timeout": 1},
            hide_parameters=True,
        )
        broken_sessions = async_sessionmaker(broken)
        try:
            async with rig(run_database) as fixture:
                original, run = await seeded_credentials(fixture)
                now = datetime.now(UTC)
                bad_checkpoint = AttemptCheckpoint(
                    lambda: SqlAlchemyRunLifecycleUnitOfWork(broken_sessions),
                    run,
                    now + timedelta(minutes=1),
                    lambda: now,
                )
                good_checkpoint = AttemptCheckpoint(
                    lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                    run,
                    now + timedelta(minutes=1),
                    lambda: now,
                )
                calls = 0

                async def actual_checkpoint() -> None:
                    nonlocal calls
                    calls += 1
                    if stage == "first_checkpoint" or stage == "final_checkpoint" and calls == 2:
                        await bad_checkpoint()
                    else:
                        await good_checkpoint()

                credentials = GitLabRunCredentials(
                    session_factory=broken_sessions
                    if stage == "persisted_read"
                    else fixture.sessions,
                    authority=fixture.authority,
                    run_id=run,
                    expected_workspace_id=fixture.workspace,
                    expected_input=original.expected_input,
                    checkpoint=actual_checkpoint,
                )
                with pytest.raises(RequiredSourceUnavailable) as error:
                    await credentials.credential_for_request(
                        error_code="commit_messages_fetch_failed"
                    )
                assert error.value.error_code == "commit_messages_fetch_failed"
                assert error.value.message == "Required review source authority is unavailable"
        finally:
            await broken.dispose()

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_codec_corruption_is_required_unavailable_not_false_cancellation(
    run_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy import text

    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable

    async def scenario() -> None:
        async with rig(run_database) as fixture:
            credentials, _ = await seeded_credentials(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET token_ciphertext=:cipher "
                        "WHERE provider_installation_id=:id"
                    ),
                    {
                        "cipher": b"corrupt-synthetic-canary",
                        "id": credentials.expected_input.origin_provider_installation_id,
                    },
                )
            with pytest.raises(RequiredSourceUnavailable) as error:
                await credentials.credential_for_request()
            assert "canary" not in str(error.value)

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_concurrent_first_admission_pins_one_generation_and_later_attempt_may_rotate(
    run_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(run_database) as fixture:
            credentials, run = await seeded_credentials(fixture)
            first, second = await asyncio.gather(
                credentials.credential_for_request(), credentials.credential_for_request()
            )
            assert first.target == second.target
            await fixture.lifecycle.rotate_token(
                installation_id=first.target.origin_provider_installation_id,
                token="replacement-authority-token",
                expected_generation=1,
            )
            with pytest.raises(RunCancelled):
                await credentials.credential_for_request()
            fresh = GitLabRunCredentials(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                run_id=run,
                expected_workspace_id=fixture.workspace,
                expected_input=credentials.expected_input,
                checkpoint=checkpoint,
            )
            assert (await fresh.credential_for_request()).token == "replacement-authority-token"

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("status", [200, 404, 429, 500])
def test_real_rotation_during_error_reply_is_control_not_optional_http(
    run_database: tuple[str, str, UUID], status: int
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
    from app.modules.integrations.gitlab.infrastructure.run_http import (
        GitLabMergeRequestResource,
        GitLabRunHttp,
    )
    from tests.test_gitlab_run_http import Stream

    async def scenario() -> None:
        async with rig(run_database) as fixture:
            credentials, _ = await seeded_credentials(fixture)
            stream = Stream((b"{invalid",))

            async def external(request: httpx.Request) -> httpx.Response:
                origin = credentials.expected_input.origin_provider_installation_id
                assert origin is not None
                await fixture.lifecycle.rotate_token(
                    installation_id=origin,
                    token="replacement-authority-token",
                    expected_generation=1,
                )
                return httpx.Response(
                    status, headers={"Content-Type": "application/json"}, stream=stream
                )

            async with GitLabRunHttp(
                credentials=credentials,
                origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                transport=httpx.MockTransport(external),
            ) as http:
                with pytest.raises(RunCancelled):
                    await http.get_json(GitLabMergeRequestResource(), max_bytes=1024)
            assert stream.closed

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_same_numeric_project_in_other_tenant_never_supplies_this_run_credentials(
    run_database: tuple[str, str, UUID],
) -> None:
    from uuid import uuid4

    from sqlalchemy import text

    async def scenario() -> None:
        async with rig(run_database) as fixture:
            credentials, run = await seeded_credentials(fixture)
            other_workspace = uuid4()
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id,name,daily_budget_usd) "
                        "VALUES (:id,'other tenant',7)"
                    ),
                    {"id": other_workspace},
                )
            other = await fixture.seed(
                workspace=other_workspace, origin="https://other.gitlab.example"
            )
            assert other.project_id == 17
            assert (
                await credentials.credential_for_request()
            ).target.workspace_id == fixture.workspace
            forged = GitLabRunCredentials(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                run_id=run,
                expected_workspace_id=other_workspace,
                expected_input=credentials.expected_input,
                checkpoint=checkpoint,
            )
            with pytest.raises(RunCancelled):
                await forged.credential_for_request()
            await fixture.lifecycle.revoke_installation(
                installation_id=other.origin_provider_installation_id
            )
            assert (
                await credentials.credential_for_request()
            ).target.workspace_id == fixture.workspace

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("control", ["cancel", "deadline", "required", "task", "programmer"])
def test_admission_preserves_control_and_programmer_failures(
    run_database: tuple[str, str, UUID], control: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.modules.reviews.application.handle_review_run import AttemptCheckpoint
    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable, RunFailure
    from app.modules.reviews.infrastructure.run_lifecycle_store import (
        SqlAlchemyRunLifecycleUnitOfWork,
    )

    async def scenario() -> None:
        async with rig(run_database) as fixture:
            original, run = await seeded_credentials(fixture)
            primary: BaseException = {
                "cancel": RunCancelled("stopped"),
                "deadline": RunFailure("deadline_exceeded"),
                "required": RequiredSourceUnavailable("diff_fetch_failed"),
                "task": asyncio.CancelledError(),
                "programmer": RuntimeError("programmer"),
            }[control]
            now = datetime.now(UTC)
            expired = AttemptCheckpoint(
                lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                run,
                now - timedelta(seconds=1),
                lambda: now,
            )

            async def control_checkpoint() -> None:
                if control == "deadline":
                    await expired()
                raise primary

            credentials = GitLabRunCredentials(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                run_id=run,
                expected_workspace_id=fixture.workspace,
                expected_input=original.expected_input,
                checkpoint=control_checkpoint,
            )
            with pytest.raises(type(primary)) as error:
                await credentials.credential_for_request()
            if control == "deadline":
                assert (
                    isinstance(error.value, RunFailure)
                    and error.value.error_code == "deadline_exceeded"
                )
            else:
                assert error.value is primary

    asyncio.run(asyncio.wait_for(scenario(), 25))
