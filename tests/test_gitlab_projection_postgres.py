"""Public GitLab projection/receipt flows on real PostgreSQL and synthetic HTTP."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabOriginPolicy,
)
from app.modules.integrations.gitlab.application.request_credentials import (
    GitLabCredentialGenerationChanged,
    GitLabRequestUnavailable,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.infrastructure.current_merge_request import (
    HttpGitLabCurrentMergeRequestProvider,
)
from app.modules.reviews.application.project_gitlab_merge_request import (
    ProjectGitLabMergeRequest,
)
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionLock,
    SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
)
from tests.gitlab_projection_support import (
    _ORIGIN,
    _TOKEN,
    NOW,
    assert_no_sql_transaction,
    canonical,
    command_for,
    lock_for,
    project,
    response,
    rig,
    run_json,
    seed_run,
)

pytest_plugins = ("tests.gitlab_projection_support",)
pytestmark = pytest.mark.integration


def test_whole_projection_uses_pool_one_and_canonical_start_not_mergebase(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            url, schema, _ = authority_database
            lock = SqlAlchemyGitLabMergeRequestProjectionLock(
                url, connect_args={"options": f"-csearch_path={schema}"}
            )
            try:
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: response(canonical(target)))
                ) as client:
                    result = await asyncio.wait_for(
                        project(fixture, client, lock).execute(command_for(target)), 5
                    )
                assert result.target == target
                assert result.current_head == "a" * 40
                assert result.created and result.head_changed
                async with fixture.sessions() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT head_sha,base_sha,title,"
                                "state,ai_review_labeled,reviewer_requested FROM code_changes"
                            )
                        )
                    ).one()
                    assert tuple(row) == (
                        "a" * 40,
                        "b" * 40,
                        "Canonical title",
                        "open",
                        False,
                        False,
                    )
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
            finally:
                await lock.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("launch_order", [("A", "B"), ("B", "A")])
@pytest.mark.parametrize("last_state", ["opened", "closed", "merged"])
def test_equal_timestamp_contender_blocks_before_http_then_fetches_current(
    authority_database: tuple[str, str, UUID],
    launch_order: tuple[str, str],
    last_state: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            captured, release = asyncio.Event(), asyncio.Event()
            visits: list[str] = []

            async def handle(request: httpx.Request) -> httpx.Response:
                task = asyncio.current_task()
                assert task is not None
                visits.append(task.get_name())
                await assert_no_sql_transaction(authority_database[0])
                if len(visits) == 1:
                    old = canonical(target)
                    captured.set()
                    await release.wait()
                    return response(old)
                return response(canonical(target, head="d" * 40, state=last_state))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                use_case = project(fixture, client, lock)
                first = asyncio.create_task(
                    use_case.execute(command_for(target)), name=launch_order[0]
                )
                await asyncio.wait_for(captured.wait(), 2)
                second = asyncio.create_task(
                    use_case.execute(command_for(target)), name=launch_order[1]
                )
                await asyncio.sleep(0.05)
                assert visits == [launch_order[0]]
                assert not second.done()
                release.set()
                outcomes = await asyncio.wait_for(asyncio.gather(first, second), 5)
            assert outcomes[0].code_change_id == outcomes[1].code_change_id
            assert outcomes[0].created and not outcomes[1].created
            assert visits == list(launch_order)
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text("SELECT head_sha,state,count(*) OVER() FROM code_changes")
                    )
                ).one()
                assert tuple(row) == ("d" * 40, "open" if last_state == "opened" else last_state, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["rotate", "revoke", "unbind", "disable"])
def test_lifecycle_during_http_finishes_without_deadlock_and_refuses_final_write(
    authority_database: tuple[str, str, UUID],
    operation: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            captured, release = asyncio.Event(), asyncio.Event()

            async def handle(request: httpx.Request) -> httpx.Response:
                captured.set()
                await release.wait()
                return response(canonical(target))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                task = asyncio.create_task(
                    project(fixture, client, lock).execute(command_for(target))
                )
                await asyncio.wait_for(captured.wait(), 2)
                await assert_no_sql_transaction(authority_database[0])
                if operation == "rotate":
                    await asyncio.wait_for(
                        fixture.lifecycle.rotate_token(
                            installation_id=target.origin_provider_installation_id,
                            token="replacement-authority-token",
                        ),
                        2,
                    )
                elif operation == "revoke":
                    await asyncio.wait_for(
                        fixture.lifecycle.revoke_installation(
                            installation_id=target.origin_provider_installation_id
                        ),
                        2,
                    )
                else:
                    # Fixture-labelled authority states; no new management feature is implied.
                    async with fixture.sessions.begin() as session:
                        if operation == "unbind":
                            await session.execute(
                                text("DELETE FROM gitlab_project_bindings WHERE repository_id=:id"),
                                {"id": target.repository_id},
                            )
                        else:
                            await session.execute(
                                text("UPDATE repositories SET enabled=false WHERE id=:id"),
                                {"id": target.repository_id},
                            )
                release.set()
                expected = (
                    GitLabCredentialGenerationChanged
                    if operation == "rotate"
                    else GitLabTargetInactive
                )
                with pytest.raises(expected):
                    await asyncio.wait_for(task, 2)
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(scenario())


def test_borrowed_authority_uses_callers_pool_one_transaction_and_never_commits_or_closes(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            async with fixture.sessions() as session:
                await session.execute(
                    text("UPDATE workspaces SET name='caller rollback' WHERE id=:id"),
                    {"id": fixture.workspace},
                )
                await asyncio.wait_for(fixture.reader.checkpoint_in_session(session, target), 1)
                assert session.in_transaction()
                with pytest.raises(GitLabCredentialGenerationChanged):
                    await fixture.reader.checkpoint_in_session(
                        session, replace(target, credential_generation=2)
                    )
                assert session.in_transaction()
                await session.rollback()
                assert (
                    await session.scalar(
                        text("SELECT name FROM workspaces WHERE id=:id"), {"id": fixture.workspace}
                    )
                    == "synthetic authority"
                )
                await session.execute(
                    text("UPDATE workspaces SET name='caller commit' WHERE id=:id"),
                    {"id": fixture.workspace},
                )
                await fixture.reader.checkpoint_in_session(session, target)
                await session.commit()
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT name FROM workspaces WHERE id=:id"), {"id": fixture.workspace}
                    )
                    == "caller commit"
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("external_id,iid", [(902, 12), (901, 13)])
def test_conflicting_provider_id_or_iid_preserves_existing_rows(
    authority_database: tuple[str, str, UUID],
    external_id: int,
    iid: int,
) -> None:
    from app.modules.reviews.application.project_gitlab_merge_request import (
        GitLabMergeRequestIdentityConflict,
    )

    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            data = canonical(target)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(data))
            ) as client:
                use_case = project(fixture, client, lock)
                first = await use_case.execute(command_for(target))
                data = canonical(target, head="d" * 40, iid=iid, external_id=external_id)
                with pytest.raises(GitLabMergeRequestIdentityConflict):
                    await use_case.execute(command_for(target, iid=iid, external_id=external_id))
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text("SELECT id,external_id,external_number,head_sha FROM code_changes")
                    )
                ).one()
                assert tuple(row) == (first.code_change_id, 901, 12, "a" * 40)

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_kind", ["exception", "cancel", "timeout"])
def test_independent_lock_releases_after_errors_and_does_not_mask_body_exception(
    authority_database: tuple[str, str, UUID],
    exit_kind: str,
) -> None:
    async def scenario() -> None:
        async with (
            rig(authority_database) as fixture,
            lock_for(authority_database, timeout=0.05) as first,
            lock_for(authority_database, timeout=0.05) as second,
        ):
            target = await fixture.seed()
            command = command_for(target)
            if exit_kind == "exception":
                with pytest.raises(LookupError, match="body sentinel"):
                    async with first.hold(command):
                        raise LookupError("body sentinel")
            elif exit_kind == "cancel":
                entered = asyncio.Event()

                async def holding() -> None:
                    async with first.hold(command):
                        entered.set()
                        await asyncio.Event().wait()

                task = asyncio.create_task(holding())
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                async with first.hold(command):
                    with pytest.raises(
                        GitLabRequestUnavailable, match="^gitlab_request_unavailable$"
                    ):
                        async with second.hold(command):
                            raise AssertionError("same key may not acquire")
            async with second.hold(command):
                await assert_no_sql_transaction(authority_database[0])
            observer = create_async_engine(authority_database[0], poolclass=NullPool)
            try:
                async with observer.connect() as connection:
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE "
                                "application_name='t8c-lock'"
                            )
                        )
                        == 0
                    )
            finally:
                await observer.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("dimension", ["origin", "project", "iid"])
def test_advisory_identity_dimensions_do_not_block_each_other(
    authority_database: tuple[str, str, UUID],
    dimension: str,
) -> None:
    async def scenario() -> None:
        async with (
            rig(authority_database) as fixture,
            lock_for(authority_database, timeout=0.05) as first,
            lock_for(authority_database, timeout=0.05) as second,
        ):
            target = await fixture.seed()
            command = command_for(target)
            other = replace(
                command,
                **(
                    {"origin_provider_installation_id": uuid4()}
                    if dimension == "origin"
                    else {"project_id": 18}
                    if dimension == "project"
                    else {"iid": 13}
                ),
            )
            async with first.hold(command), second.hold(other):
                await assert_no_sql_transaction(authority_database[0])

    asyncio.run(scenario())


@pytest.mark.parametrize("current_state", ["closed", "merged", "newhead"])
@pytest.mark.parametrize(
    "run_state,attempt",
    [
        ("queued", 0),
        ("queued", 1),
        ("running", 1),
        ("publishing", 2),
        ("succeeded", 1),
        ("failed", 1),
        ("cancelled", 1),
        ("skipped", 1),
    ],
)
def test_canonical_cancellation_flags_and_transactional_notices(
    authority_database: tuple[str, str, UUID],
    current_state: str,
    run_state: str,
    attempt: int,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            data = canonical(target)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(data))
            ) as client:
                use_case = project(fixture, client, lock)
                result = await use_case.execute(command_for(target))
                run = await seed_run(
                    fixture, target, result.code_change_id, state=run_state, attempt=attempt
                )
                terminal = await seed_run(fixture, target, result.code_change_id, state="succeeded")
                history = await run_json(fixture, terminal)
                before = await run_json(fixture, run)
                data = canonical(
                    target,
                    head="d" * 40 if current_state == "newhead" else "a" * 40,
                    state="opened" if current_state == "newhead" else current_state,
                )
                with psycopg.connect(
                    authority_database[0].replace("postgresql+psycopg://", "postgresql://"),
                    autocommit=True,
                ) as listener:
                    listener.execute("LISTEN run_updated")
                    await use_case.execute(command_for(target))
                    notices = [json.loads(note.payload) for note in listener.notifies(timeout=0.05)]
                assert await run_json(fixture, terminal) == history
                async with fixture.sessions() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT state,error_code,cancel_requested,"
                                "finished_at,cancellation_signal_requested_at FROM runs WHERE "
                                "id=:id"
                            ),
                            {"id": run},
                        )
                    ).one()
                if run_state == "queued":
                    assert tuple(row) == (
                        "cancelled",
                        "superseded" if current_state == "newhead" else "pr_closed",
                        False,
                        NOW,
                        NOW if attempt else None,
                    )
                    assert notices == [
                        {
                            "run_id": str(run),
                            "workspace_id": str(target.workspace_id),
                            "status": "cancelled",
                        }
                    ]
                elif run_state in {"running", "publishing"}:
                    assert tuple(row) == (run_state, None, True, None, None)
                    assert notices == []
                else:
                    assert await run_json(fixture, run) == before
                    assert notices == []

    asyncio.run(scenario())


def test_superseded_does_not_cancel_already_current_head_work(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            data = canonical(target)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(data))
            ) as client:
                use_case = project(fixture, client, lock)
                result = await use_case.execute(command_for(target))
                run = await seed_run(fixture, target, result.code_change_id, head="d" * 40)
                before = await run_json(fixture, run)
                data = canonical(target, head="d" * 40)
                await use_case.execute(command_for(target))
                assert await run_json(fixture, run) == before

    asyncio.run(scenario())


def test_replacement_origin_cannot_cancel_or_notify_historical_origin_run(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            old = await fixture.seed()
            data = canonical(old)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(data))
            ) as client:
                result = await project(fixture, client, lock).execute(command_for(old))
                run = await seed_run(fixture, old, result.code_change_id, attempt=1)
                history = await run_json(fixture, run)
                other_workspace, replacement = uuid4(), uuid4()
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO workspaces (id,name,daily_budget_usd) VALUES "
                            "(:id,'replacement',7)"
                        ),
                        {"id": other_workspace},
                    )
                await fixture.management.create(
                    installation_id=replacement,
                    workspace_id=other_workspace,
                    instance_origin="https://other.gitlab.example",
                    token=_TOKEN,
                    webhook_secret="s" * 32,
                )
                # Fixture-only reservation replacement; historical Run.origin remains old.
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("DELETE FROM gitlab_project_bindings WHERE repository_id=:id"),
                        {"id": old.repository_id},
                    )
                    await session.execute(
                        text(
                            "UPDATE repositories SET provider_installation_id=:replacement WHERE "
                            "id=:id"
                        ),
                        {"id": old.repository_id, "replacement": replacement},
                    )
                    await session.execute(
                        text(
                            "UPDATE gitlab_connections SET intake_enabled=true WHERE "
                            "provider_installation_id=:id"
                        ),
                        {"id": replacement},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO gitlab_project_bindings "
                            "(repository_id,provider_installation_id,instance_origin,project_exte"
                            "rnal_id) VALUES (:repo,:origin,'https://other.gitlab.example',17)"
                        ),
                        {"repo": old.repository_id, "origin": replacement},
                    )
                target = replace(
                    old,
                    origin_provider_installation_id=replacement,
                    workspace_id=other_workspace,
                    instance_origin="https://other.gitlab.example",
                )
                data = canonical(target, state="closed")
                with psycopg.connect(
                    authority_database[0].replace("postgresql+psycopg://", "postgresql://"),
                    autocommit=True,
                ) as listener:
                    listener.execute("LISTEN run_updated")
                    projected = await project(fixture, client, lock).execute(command_for(target))
                    assert list(listener.notifies(timeout=0.05)) == []
                assert projected.code_change_id == result.code_change_id
                assert await run_json(fixture, run) == history

    asyncio.run(scenario())


def test_projection_and_cancellation_rollback_emits_no_notice(
    authority_database: tuple[str, str, UUID],
) -> None:
    class RefuseCommit(SqlAlchemyGitLabMergeRequestProjectionUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("synthetic commit failure")

    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            data = canonical(target)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(data))
            ) as client:
                result = await project(fixture, client, lock).execute(command_for(target))
                run = await seed_run(fixture, target, result.code_change_id, attempt=1)
                before = await run_json(fixture, run)
                data = canonical(target, state="closed")
                use_case = ProjectGitLabMergeRequest(
                    authority=fixture.reader,
                    current_provider=HttpGitLabCurrentMergeRequestProvider(
                        client=client,
                        authority=fixture.authority,
                        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
                    ),
                    projection_lock=lock,
                    uow_factory=lambda: RefuseCommit(fixture.sessions),
                    now=lambda: NOW,
                )
                with psycopg.connect(
                    authority_database[0].replace("postgresql+psycopg://", "postgresql://"),
                    autocommit=True,
                ) as listener:
                    listener.execute("LISTEN run_updated")
                    with pytest.raises(RuntimeError, match="synthetic commit failure"):
                        await use_case.execute(command_for(target))
                    assert list(listener.notifies(timeout=0.05)) == []
                assert await run_json(fixture, run) == before
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT state FROM code_changes WHERE id=:id"),
                            {"id": result.code_change_id},
                        )
                        == "open"
                    )

    asyncio.run(scenario())
