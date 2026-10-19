"""Public GitLab projection/receipt flows on real PostgreSQL and synthetic HTTP."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.gitlab.api.dispatch import GitLabWebhookDispatchAdapter, action_of
from app.modules.integrations.gitlab.application.eligibility import DetermineGitLabEligibility
from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabConnectionLifecycle,
    GitLabOriginPolicy,
    ManageGitLabConnection,
)
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabRequestCredentials,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.application.webhook_receipts import prepare_gitlab_receipt
from app.modules.integrations.gitlab.infrastructure.connection_store import (
    CredentialCipherAdapter,
    SqlAlchemyGitLabManagementUnitOfWork,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.current_merge_request import (
    HttpGitLabCurrentMergeRequestProvider,
)
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.integrations.gitlab.infrastructure.webhook_receipts import (
    SqlAlchemyGitLabWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveWebhookDelivery,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_gitlab_merge_request import (
    GitLabMergeRequestCommand,
    GitLabMergeRequestHandler,
    GitLabMergeRequestHandlingResult,
    GitLabMergeRequestProjectionResult,
    HandleGitLabMergeRequest,
    ProjectGitLabMergeRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    PendingRunMessage,
    RunMessagePublisher,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.gitlab_eligibility_candidates import (
    SqlAlchemyGitLabEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionLock,
    SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork

pytestmark = pytest.mark.integration
_TOKEN = "synthetic-authority-PAT-canary"
_ORIGIN = "https://gitlab.com"


@pytest.fixture
def authority_database() -> Iterator[tuple[str, str, UUID]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_projection_{uuid4().hex}"
    workspace = uuid4()
    engine = create_engine(database_url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.commit()
            try:
                connection.execute(text(f'SET search_path TO "{schema}"'))
                connection.commit()
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id,name,daily_budget_usd) "
                        "VALUES (:id,'synthetic authority',7)"
                    ),
                    {"id": workspace},
                )
                connection.execute(
                    text(
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.system',1,'synthetic prompt',:checksum,true)"
                    ),
                    {"id": uuid4(), "checksum": "a" * 64},
                )
                connection.commit()
                yield database_url, schema, workspace
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


class Projects:
    async def authenticate(self, *, origin: str, token: str) -> None:
        assert origin in {_ORIGIN, "https://other.gitlab.example"}
        assert token in {_TOKEN, "replacement-authority-token"}

    async def fetch_project(
        self,
        *,
        origin: str,
        token: str,
        project_id: int,
    ) -> RepositoryOnboardingInput:
        await self.authenticate(origin=origin, token=token)
        return RepositoryOnboardingInput(
            RepositorySnapshot(project_id, "team/project", "main", f"{origin}/team/project"),
            {"Python": 100},
        )


@dataclass
class Rig:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    codec: CredentialCipherAdapter
    reader: SqlAlchemyGitLabTargetReader
    authority: GitLabRequestCredentials
    management: ManageGitLabConnection
    lifecycle: GitLabConnectionLifecycle
    workspace: UUID

    async def seed(
        self, *, origin: str = _ORIGIN, workspace: UUID | None = None
    ) -> BoundGitLabTarget:
        installation = uuid4()
        workspace = workspace or self.workspace
        await self.management.create(
            installation_id=installation,
            workspace_id=workspace,
            instance_origin=origin,
            token=_TOKEN,
            webhook_secret="s" * 32,
        )
        bound = await self.management.bind_project(installation_id=installation, project_id=17)
        # Fixture-only synthetic enabled state. No production enable operation is introduced.
        async with self.sessions() as session:
            await session.execute(
                text(
                    "UPDATE gitlab_connections SET intake_enabled=true "
                    "WHERE provider_installation_id=:id"
                ),
                {"id": installation},
            )
            await session.commit()
        return BoundGitLabTarget(installation, workspace, bound.repository_id, origin, 17, 1)


@asynccontextmanager
async def rig(database: tuple[str, str, UUID]) -> AsyncIterator[Rig]:
    database_url, schema, workspace = database
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        hide_parameters=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    codec = CredentialCipherAdapter(
        CredentialCipher(active_key_id="test-key", keys={"test-key": b"k" * 32})
    )
    reader = SqlAlchemyGitLabTargetReader(sessions)
    policy = GitLabOriginPolicy(frozenset({_ORIGIN, "https://other.gitlab.example"}))
    try:
        yield Rig(
            engine,
            sessions,
            codec,
            reader,
            GitLabRequestCredentials(reader, codec),
            ManageGitLabConnection(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
                codec=codec,
                projects=Projects(),
                origin_policy=policy,
                rule_sets=load_default_rule_sets(Path("review/rules")),
            ),
            GitLabConnectionLifecycle(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
                codec=codec,
                projects=Projects(),
                origin_policy=policy,
            ),
            workspace,
        )
    finally:
        await engine.dispose()


NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def command_for(
    target: BoundGitLabTarget, *, iid: int = 12, external_id: int = 901, action: str = "open"
) -> GitLabMergeRequestCommand:
    return GitLabMergeRequestCommand(
        target.origin_provider_installation_id, target.project_id, external_id, iid, action
    )


def canonical(
    target: BoundGitLabTarget,
    *,
    head: str = "a" * 40,
    state: str = "opened",
    iid: int = 12,
    external_id: int = 901,
) -> dict[str, Any]:
    return {
        "id": external_id,
        "iid": iid,
        "project_id": target.project_id,
        "target_project_id": target.project_id,
        "source_project_id": 29,
        "state": state,
        "title": "Canonical title",
        "description": "Canonical description",
        "author": {"username": "canonical-author"},
        "web_url": f"{target.instance_origin}/team/project/-/merge_requests/{iid}",
        "source_branch": "feature",
        "target_branch": "main",
        "updated_at": NOW.isoformat(),
        "diff_refs": {"head_sha": head, "start_sha": "b" * 40, "base_sha": "c" * 40},
    }


class Body(httpx.AsyncByteStream):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = json.dumps(data).encode()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


def response(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Body(data))


def project(
    fixture: Rig,
    client: httpx.AsyncClient,
    lock: SqlAlchemyGitLabMergeRequestProjectionLock,
    *,
    now: Callable[[], datetime] = lambda: NOW,
) -> ProjectGitLabMergeRequest:
    return ProjectGitLabMergeRequest(
        authority=fixture.reader,
        current_provider=HttpGitLabCurrentMergeRequestProvider(
            client=client,
            authority=fixture.authority,
            origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN, "https://other.gitlab.example"})),
        ),
        projection_lock=lock,
        uow_factory=lambda: SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(fixture.sessions),
        now=now,
    )


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


@asynccontextmanager
async def lock_for(
    database: tuple[str, str, UUID], *, timeout: float = 1
) -> AsyncIterator[SqlAlchemyGitLabMergeRequestProjectionLock]:
    url, schema, _ = database
    lock = SqlAlchemyGitLabMergeRequestProjectionLock(
        url,
        connect_args={"options": f"-csearch_path={schema}", "application_name": "t8c-lock"},
        lock_timeout_seconds=timeout,
    )
    try:
        yield lock
    finally:
        await lock.aclose()


async def assert_no_sql_transaction(url: str) -> None:
    observer = create_async_engine(url, poolclass=NullPool)
    try:
        async with observer.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT application_name,xact_start "
                        "FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid<>pg_backend_pid() AND backend_type='client backend'"
                    )
                )
            ).all()
            assert all(row.xact_start is None for row in rows), rows
            assert any(row.application_name == "t8c-lock" for row in rows)
    finally:
        await observer.dispose()


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


async def seed_run(
    fixture: Rig,
    target: BoundGitLabTarget,
    change_id: UUID,
    *,
    state: str = "queued",
    attempt: int = 0,
    head: str = "a" * 40,
    origin: UUID | None = None,
) -> UUID:
    run = uuid4()
    async with fixture.sessions.begin() as session:
        rule = await session.scalar(
            text("SELECT id FROM rule_versions WHERE repository_id=:id AND is_active"),
            {"id": target.repository_id},
        )
        prompt = await session.scalar(text("SELECT id FROM prompt_versions WHERE is_active"))
        await session.execute(
            text(
                "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
                "base_sha,head_sha,state,trigger,idempotency_key,engine,rule_version_id,prompt_ve"
                "rsion_id,"
                "attempt,available_at,error_code,finished_at) VALUES "
                "(:id,:change,:origin,:base,:head,"
                "CAST(:state AS run_state),'manual',:key,'fast',:rule,:prompt,:attempt,:now,"
                "CASE WHEN :terminal THEN 'history' ELSE NULL END,CASE WHEN :terminal THEN :now "
                "ELSE NULL END)"
            ),
            {
                "id": run,
                "change": change_id,
                "origin": origin or target.origin_provider_installation_id,
                "base": "b" * 40,
                "head": head,
                "state": state,
                "key": uuid4().hex * 2,
                "rule": rule,
                "prompt": prompt,
                "attempt": attempt,
                "now": NOW,
                "terminal": state in {"succeeded", "failed", "cancelled", "skipped"},
            },
        )
    return run


async def run_json(fixture: Rig, run: UUID) -> object:
    async with fixture.sessions() as session:
        return await session.scalar(
            text("SELECT row_to_json(r)::text FROM runs r WHERE id=:id"), {"id": run}
        )


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


@dataclass
class Clock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


def receiver(
    fixture: Rig, handler: GitLabMergeRequestHandler, clock: Clock
) -> ReceiveWebhookDelivery:
    return ReceiveWebhookDelivery(
        provider="gitlab",
        uow_factory=lambda: SqlAlchemyGitLabWebhookReceiptUnitOfWork(fixture.sessions),
        dispatcher=GitLabWebhookDispatchAdapter(handler),
        now=clock,
        action_of=action_of,
    )


def receipt_for(target: BoundGitLabTarget, key: str = "stable-id", action: str = "open") -> Any:
    return prepare_gitlab_receipt(
        connection_id=target.origin_provider_installation_id,
        event_name="Merge Request Hook",
        webhook_id=key,
        payload_json=json.dumps(
            {
                "object_kind": "merge_request",
                "project": {"id": 17},
                "object_attributes": {
                    "id": 901,
                    "iid": 12,
                    "action": action,
                    "title": "untrusted title",
                    "head_sha": "wrong",
                    "state": "wrong",
                    "url": "https://evil.invalid",
                },
                "workspace_id": str(uuid4()),
                "installation": {"id": 999},
            }
        ),
    )


def test_real_receipt_claim_is_committed_before_http_and_distinct_samehead_replays_are_idempotent(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            clock, calls = Clock(), []
            data = canonical(target)

            async def handle(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                await assert_no_sql_transaction(authority_database[0])
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM webhook_events "
                                "WHERE provider='gitlab' AND projection_claim_token IS NOT NULL "
                                "AND projection_lease_until IS NOT NULL AND projected_at IS NULL"
                            )
                        )
                        == 1
                    )
                return response(data)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                engine = receiver(fixture, project(fixture, client, lock), clock)
                delivery = receipt_for(target)
                assert await engine.execute(delivery) == InstallationDeliveryDispatchStatus.PENDING
                assert (
                    await engine.execute(delivery) == InstallationDeliveryDispatchStatus.DUPLICATE
                )
                assert await engine.replay_pending() == 1
                async with fixture.sessions() as session:
                    first = (
                        await session.execute(
                            text("SELECT id,head_first_seen_at FROM code_changes")
                        )
                    ).one()
                data["title"] = "Canonical metadata update"
                assert (
                    await engine.execute(receipt_for(target, "another-id"))
                    == InstallationDeliveryDispatchStatus.PENDING
                )
                assert await engine.replay_pending() == 1
                assert await engine.replay_pending() == 0
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id,head_first_seen_at,title,head_sha,base_sha FROM code_changes"
                        )
                    )
                ).one()
                assert tuple(row) == (
                    first.id,
                    first.head_first_seen_at,
                    "Canonical metadata update",
                    "a" * 40,
                    "b" * 40,
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL "
                            "AND projection_claim_token IS NULL"
                        )
                    )
                    == 2
                )
                assert (
                    await session.scalar(
                        text("SELECT delivery_id FROM webhook_events WHERE delivery_id=:id"),
                        {"id": delivery.delivery_id},
                    )
                    == delivery.delivery_id
                )
            assert len(calls) == 2

    asyncio.run(scenario())


def test_valid_unknown_action_receipt_finishes_without_canonical_http(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()

            def forbidden(request: httpx.Request) -> httpx.Response:
                raise AssertionError("irrelevant action must never reach HTTP")

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                engine = receiver(fixture, project(fixture, client, lock), Clock())
                await engine.execute(receipt_for(target, action="approved"))
                assert await engine.replay_pending() == 1
                assert await engine.replay_pending() == 0
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT projected_at IS NOT NULL FROM webhook_events")
                    )
                    is True
                )
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["notready", "generation"])
def test_real_receipt_exhausts_existing_three_attempt_deferral_or_failure_budget(
    authority_database: tuple[str, str, UUID],
    failure: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            clock, calls = Clock(), []

            async def handle(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                await assert_no_sql_transaction(authority_database[0])
                data = canonical(target)
                if failure == "notready":
                    data["diff_refs"] = None
                else:
                    await fixture.lifecycle.rotate_token(
                        installation_id=target.origin_provider_installation_id,
                        token="replacement-authority-token",
                    )
                return response(data)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                engine = receiver(fixture, project(fixture, client, lock), clock)
                await engine.execute(receipt_for(target))
                for _ in range(3):
                    handled = await engine.replay_pending()
                    assert handled == (1 if failure == "notready" else 0)
                    clock.value += timedelta(minutes=6)
                assert await engine.replay_pending() == 0
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT projection_attempt_count,projection_deferred_at IS NOT NULL,"
                            "projection_failed_at IS NOT NULL,projection_claim_token,retry_after "
                            "FROM webhook_events"
                        )
                    )
                ).one()
                assert tuple(row) == (3, failure == "notready", failure == "generation", None, None)
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
            assert len(calls) == (9 if failure == "notready" else 3)

    asyncio.run(scenario())


def test_crash_after_projection_before_finalize_reclaims_and_preserves_one_projection(
    authority_database: tuple[str, str, UUID],
) -> None:
    class CrashAfterProjection:
        def __init__(self, handler: GitLabMergeRequestHandler) -> None:
            self.handler = handler

        async def execute(
            self, command: GitLabMergeRequestCommand
        ) -> GitLabMergeRequestProjectionResult:
            await self.handler.execute(command)
            raise asyncio.CancelledError()

    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock = await fixture.seed(), Clock()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(canonical(target)))
            ) as client:
                use_case = project(fixture, client, lock)
                crashing = receiver(fixture, CrashAfterProjection(use_case), clock)
                await crashing.execute(receipt_for(target))
                with pytest.raises(asyncio.CancelledError):
                    await crashing.replay_pending()
                async with fixture.sessions() as session:
                    first = (
                        await session.execute(
                            text("SELECT id,head_first_seen_at FROM code_changes")
                        )
                    ).one()
                    assert (
                        await session.scalar(
                            text(
                                "SELECT projection_claim_token IS NOT NULL AND projected_at IS "
                                "NULL FROM webhook_events"
                            )
                        )
                        is True
                    )
                clock.value += timedelta(minutes=6)
                retrying = receiver(fixture, use_case, clock)
                assert await retrying.replay_pending() == 1
                assert await retrying.replay_pending() == 0
                async with fixture.sessions() as session:
                    assert tuple(
                        (
                            await session.execute(
                                text("SELECT id,head_first_seen_at FROM code_changes")
                            )
                        ).one()
                    ) == tuple(first)
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
                    assert (
                        await session.scalar(
                            text(
                                "SELECT projected_at IS NOT NULL AND projection_claim_token IS "
                                "NULL FROM webhook_events"
                            )
                        )
                        is True
                    )

    asyncio.run(scenario())


def test_revoked_receipt_origin_cannot_adopt_replacement_or_github_numeric_collision(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            old, clock = await fixture.seed(), Clock()

            def forbidden(request: httpx.Request) -> httpx.Response:
                raise AssertionError("retired origin cannot reach HTTP")

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                engine = receiver(fixture, project(fixture, client, lock), clock)
                await engine.execute(receipt_for(old))
                await fixture.lifecycle.revoke_installation(
                    installation_id=old.origin_provider_installation_id
                )
                replacement = await fixture.seed()
                async with fixture.sessions.begin() as session:
                    number = await session.scalar(
                        text("SELECT external_id FROM provider_installations WHERE id=:id"),
                        {"id": old.origin_provider_installation_id},
                    )
                    github = uuid4()
                    await session.execute(
                        text(
                            "INSERT INTO provider_installations "
                            "(id,workspace_id,provider,external_id,metadata) VALUES "
                            "(:id,:workspace,'github',:number,'{}')"
                        ),
                        {"id": github, "workspace": fixture.workspace, "number": number},
                    )
                assert await engine.replay_pending() == 0
                with pytest.raises(GitLabTargetInactive):
                    await project(fixture, client, lock).execute(
                        replace(command_for(old), origin_provider_installation_id=github)
                    )
                assert (
                    await fixture.reader.resolve_target(
                        GitLabTargetIdentity(replacement.origin_provider_installation_id, 17)
                    )
                    == replacement
                )
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0
                    assert (
                        await session.scalar(
                            text("SELECT projected_at IS NOT NULL FROM webhook_events")
                        )
                        is True
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
def test_final_locked_uow_refuses_authority_changed_after_all_http_checkpoints(
    authority_database: tuple[str, str, UUID],
    operation: str,
) -> None:
    from app.modules.reviews.application.project_gitlab_merge_request import (
        CanonicalGitLabMergeRequest,
        GitLabMergeRequestCheckpoint,
    )

    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(canonical(target)))
            ) as client:
                provider = HttpGitLabCurrentMergeRequestProvider(
                    client=client,
                    authority=fixture.authority,
                    origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
                )

                class ChangedAfterRead:
                    async def get_current(
                        self,
                        command: GitLabMergeRequestCommand,
                        *,
                        target: BoundGitLabTarget,
                        checkpoint: GitLabMergeRequestCheckpoint,
                    ) -> CanonicalGitLabMergeRequest:
                        current = await provider.get_current(
                            command, target=target, checkpoint=checkpoint
                        )
                        if operation == "rotate":
                            await fixture.lifecycle.rotate_token(
                                installation_id=target.origin_provider_installation_id,
                                token="replacement-authority-token",
                            )
                        else:
                            await fixture.lifecycle.revoke_installation(
                                installation_id=target.origin_provider_installation_id
                            )
                        return current

                use_case = ProjectGitLabMergeRequest(
                    authority=fixture.reader,
                    current_provider=ChangedAfterRead(),
                    projection_lock=lock,
                    uow_factory=lambda: SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(
                        fixture.sessions
                    ),
                )
                with pytest.raises(
                    GitLabCredentialGenerationChanged
                    if operation == "rotate"
                    else GitLabTargetInactive
                ):
                    await asyncio.wait_for(use_case.execute(command_for(target)), 4)
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0

    asyncio.run(scenario())


def test_two_origins_same_project_iid_use_independent_http_and_pool_one(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            first = await fixture.seed()
            second = await fixture.seed(origin="https://other.gitlab.example")
            targets = {first.instance_origin: first, second.instance_origin: second}
            visited: set[str] = set()
            both = asyncio.Event()

            async def handle(request: httpx.Request) -> httpx.Response:
                origin = f"{request.url.scheme}://{request.url.host}"
                visited.add(origin)
                if len(visited) == 2:
                    both.set()
                await both.wait()
                return response(
                    canonical(
                        targets[origin],
                        head="a" * 40 if origin == first.instance_origin else "d" * 40,
                    )
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                use_case = project(fixture, client, lock)
                results = await asyncio.wait_for(
                    asyncio.gather(
                        use_case.execute(command_for(first)), use_case.execute(command_for(second))
                    ),
                    5,
                )
                assert results[0].target == first and results[1].target == second
                assert results[0].code_change_id != results[1].code_change_id
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 2

    asyncio.run(scenario())


def test_persisted_head_clock_changes_only_when_actual_canonical_head_changes(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            clock, data = Clock(), canonical(target)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(data))
            ) as client:
                use_case = project(fixture, client, lock, now=clock)
                await use_case.execute(command_for(target))
                clock.value += timedelta(hours=1)
                data["title"] = "Later metadata"
                unchanged = await use_case.execute(command_for(target))
                assert unchanged.head_changed is False
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(text("SELECT head_first_seen_at FROM code_changes"))
                        == NOW
                    )
                clock.value += timedelta(hours=1)
                data = canonical(target, head="d" * 40)
                changed = await use_case.execute(command_for(target))
                assert changed.head_changed is True
                async with fixture.sessions() as session:
                    assert await session.scalar(
                        text("SELECT head_first_seen_at FROM code_changes")
                    ) == NOW + timedelta(hours=2)

    asyncio.run(scenario())


@pytest.mark.parametrize("body_failure", [False, True])
def test_lock_cleanup_failure_is_sanitized_and_preserves_body_failure(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
    body_failure: bool,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            original = AsyncConnection.execute

            async def fail_reset(
                self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if str(statement) == "RESET lock_timeout":
                    raise RuntimeError("RESET-SQL-DSN-PAT-canary")
                return await original(self, statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", fail_reset)
                expected = LookupError if body_failure else GitLabRequestUnavailable
                with pytest.raises(expected) as caught:
                    async with lock.hold(command_for(target)):
                        if body_failure:
                            raise LookupError("body sentinel")
                assert str(caught.value) == (
                    "body sentinel" if body_failure else "gitlab_request_unavailable"
                )
                assert caught.value.__cause__ is None
            async with lock.hold(command_for(target)):
                pass

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["insert", "commit"])
def test_public_projection_database_failure_has_safe_operational_error_and_no_write(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            scalar = AsyncSession.scalar

            async def fail_insert(
                self: AsyncSession, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if str(statement).startswith("INSERT INTO code_changes"):
                    raise RuntimeError("INSERT-SQL-DSN-PAT-canary")
                return await scalar(self, statement, *args, **kwargs)

            async def fail_commit(self: AsyncSession) -> None:
                raise RuntimeError("COMMIT-SQL-DSN-PAT-canary")

            with monkeypatch.context() as patch:
                patch.setattr(
                    AsyncSession,
                    "scalar" if stage == "insert" else "commit",
                    fail_insert if stage == "insert" else fail_commit,
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: response(canonical(target)))
                ) as client:
                    with pytest.raises(
                        GitLabRequestUnavailable, match="^gitlab_request_unavailable$"
                    ) as caught:
                        await project(fixture, client, lock).execute(command_for(target))
                    assert caught.value.__cause__ is None
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0

    asyncio.run(scenario())


def test_lock_timeout_is_reset_on_actual_backend_before_close(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with (
            rig(authority_database) as fixture,
            lock_for(authority_database, timeout=0.05) as lock,
        ):
            target = await fixture.seed()
            original = AsyncConnection.execute
            reset: list[str] = []

            async def observe(
                self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                result = await original(self, statement, *args, **kwargs)
                if str(statement) == "RESET lock_timeout":
                    reset.append((await original(self, text("SHOW lock_timeout"))).scalar_one())
                return result

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", observe)
                async with lock.hold(command_for(target)):
                    pass
            assert reset == ["0"]

    asyncio.run(scenario())


def test_cancelled_lock_waiter_closes_backend_and_later_acquisition_succeeds(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with (
            rig(authority_database) as fixture,
            lock_for(authority_database) as first,
            lock_for(authority_database) as second,
        ):
            target = await fixture.seed()
            command = command_for(target)
            async with first.hold(command):

                async def waiting() -> None:
                    async with second.hold(command):
                        raise AssertionError("blocked waiter must not acquire")

                task = asyncio.create_task(waiting())
                await asyncio.sleep(0.05)
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            async with second.hold(command):
                pass

    asyncio.run(scenario())


def test_lock_connect_failure_is_safe_operational_and_closed_lock_cannot_reopen(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()

            def fail_connect(self: AsyncEngine) -> AsyncConnection:
                raise RuntimeError("CONNECT-DSN-PAT-canary")

            with monkeypatch.context() as patch:
                patch.setattr(AsyncEngine, "connect", fail_connect)
                with pytest.raises(GitLabRequestUnavailable, match="^gitlab_request_unavailable$"):
                    async with lock.hold(command_for(target)):
                        raise AssertionError("failed connect cannot yield")
            await lock.aclose()
            with pytest.raises(GitLabRequestUnavailable):
                async with lock.hold(command_for(target)):
                    raise AssertionError("closed lock cannot acquire")

    asyncio.run(scenario())


def test_stalled_lock_cleanup_is_finite_and_releases_backend(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            original = AsyncConnection.execute

            async def stall_reset(
                self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if str(statement) == "RESET lock_timeout":
                    await asyncio.sleep(7)
                return await original(self, statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", stall_reset)
                with pytest.raises(GitLabRequestUnavailable, match="^gitlab_request_unavailable$"):
                    async with asyncio.timeout(6):
                        async with lock.hold(command_for(target)):
                            pass
            async with lock.hold(command_for(target)):
                pass

    asyncio.run(scenario())


def automatic_response(request: httpx.Request, data: dict[str, Any]) -> httpx.Response:
    # Reject GitHub, CI and unmodelled HTTP at the external boundary.
    assert request.method == "GET"
    assert request.url.scheme == "https" and request.url.host == "gitlab.com"
    assert request.url.path == "/api/v4/projects/17/merge_requests/12"
    return response(data)


@dataclass
class ConfirmedPointers:
    messages: list[PendingRunMessage]

    async def publish_confirmed(
        self,
        message: PendingRunMessage,
        *,
        kind: RunPublicationKind = RunPublicationKind.QUEUED,
    ) -> None:
        self.messages.append(message)


def automatic_enqueuer(fixture: Rig, publisher: RunMessagePublisher) -> TryEnqueueWebhookRun:
    return TryEnqueueWebhookRun(
        eligibility=DetermineGitLabEligibility(
            candidates=SqlAlchemyGitLabEligibilityCandidateStore(fixture.sessions),
        ),
        uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(fixture.sessions),
        publisher=publisher,
        now=lambda: NOW,
    )


def test_automatic_receipts_create_one_canonical_run_across_metadata_and_redelivery(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock = await fixture.seed(), Clock()
            data = canonical(target)
            pointers = ConfirmedPointers([])
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: automatic_response(request, data))
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=automatic_enqueuer(fixture, pointers),
                )
                engine = receiver(fixture, handler, clock)
                original = receipt_for(target)
                assert await engine.execute(original) == InstallationDeliveryDispatchStatus.PENDING
                assert pointers.messages == []  # admission remains receipt-only
                assert await engine.replay_pending() == 1
                data["title"] = "Canonical metadata update"
                assert await engine.execute(receipt_for(target, "metadata", "update")) == (
                    InstallationDeliveryDispatchStatus.PENDING
                )
                assert await engine.replay_pending() == 1
                assert (
                    await engine.execute(original) == InstallationDeliveryDispatchStatus.DUPLICATE
                )
                assert await engine.replay_pending() == 0
            assert len(pointers.messages) == 1
            message = pointers.messages[0]
            assert (message.provider, message.head_sha, message.base_sha) == (
                "gitlab",
                "a" * 40,
                "b" * 40,
            )
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT c.title,c.head_sha,c.base_sha,c.state,"
                            "r.origin_provider_installation_id,"
                            "r.id,r.message_published_at IS NOT NULL FROM code_changes c "
                            "JOIN runs r ON r.code_change_id=c.id"
                        )
                    )
                ).one()
                assert tuple(row) == (
                    "Canonical metadata update",
                    "a" * 40,
                    "b" * 40,
                    "open",
                    target.origin_provider_installation_id,
                    message.run_id,
                    True,
                )
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 1
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL")
                    )
                    == 2
                )

    asyncio.run(scenario())


def test_automatic_receipt_retries_deferred_head_after_old_run_even_if_projection_unchanged(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock = await fixture.seed(), Clock(datetime.now(UTC))
            data, pointers = canonical(target), ConfirmedPointers([])
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: automatic_response(request, data))
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=automatic_enqueuer(fixture, pointers),
                )
                engine = receiver(fixture, handler, clock)
                await engine.execute(receipt_for(target))
                assert await engine.replay_pending() == 1
                old = pointers.messages[0].run_id
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE runs SET state='running' WHERE id=:id"), {"id": old}
                    )
                data["diff_refs"]["head_sha"] = "d" * 40
                await engine.execute(receipt_for(target, "new-head", "update"))
                assert await engine.replay_pending() == 1
                assert len(pointers.messages) == 1
                async with fixture.sessions.begin() as session:
                    assert (
                        await session.scalar(
                            text("SELECT cancel_requested FROM runs WHERE id=:id"), {"id": old}
                        )
                        is True
                    )
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM webhook_events WHERE projected_at IS NULL")
                        )
                        == 1
                    )
                    await session.execute(
                        text("UPDATE runs SET state='cancelled',finished_at=:now WHERE id=:id"),
                        {"id": old, "now": NOW},
                    )
                clock.value += timedelta(seconds=31)
                assert await engine.replay_pending() == 1
                assert len(pointers.messages) == 2
                assert pointers.messages[1].head_sha == "d" * 40
                assert await engine.replay_pending() == 0
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 2
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM webhook_events WHERE projected_at IS NULL")
                        )
                        == 0
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["rotate", "revoke", "rebind"])
def test_automatic_handler_refuses_changed_authority_after_projection_commit_before_enqueue(
    authority_database: tuple[str, str, UUID],
    mutation: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, pointers = await fixture.seed(), ConfirmedPointers([])
            real_enqueue = automatic_enqueuer(fixture, pointers)

            class ChangeAuthority:
                async def execute(
                    self,
                    code_change_id: UUID,
                    expected_head_sha: str,
                    *,
                    expected_identity: WebhookRunIdentity | None = None,
                ) -> EnqueueResult:
                    # Public lock reacquisition proves projection hold has completed.
                    async with lock.hold(command_for(target)):
                        pass
                    async with fixture.sessions() as session:
                        assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 1
                    if mutation == "rotate":
                        await fixture.lifecycle.rotate_token(
                            installation_id=target.origin_provider_installation_id,
                            token="replacement-authority-token",
                            expected_generation=1,
                        )
                    elif mutation == "revoke":
                        await fixture.lifecycle.revoke_installation(
                            installation_id=target.origin_provider_installation_id,
                        )
                    else:
                        # Adversarial fixture-only replacement; no production rebind API.
                        replacement = await fixture.seed(origin="https://other.gitlab.example")
                        async with fixture.sessions.begin() as session:
                            await session.execute(
                                text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                                {"repo": replacement.repository_id, "id": code_change_id},
                            )
                    return await real_enqueue.execute(
                        code_change_id,
                        expected_head_sha,
                        expected_identity=expected_identity,
                    )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: automatic_response(request, canonical(target))
                )
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=ChangeAuthority(),
                )
                result = await asyncio.wait_for(handler.execute(command_for(target)), 5)
            assert result.projection.target == target
            assert result.enqueue is not None
            assert result.enqueue.status.value in {"ineligible", "stale"}
            assert pointers.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(scenario())


def test_automatic_receipt_crash_after_run_commit_reclaims_without_duplicate_run(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock, pointers = await fixture.seed(), Clock(), ConfirmedPointers([])
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: automatic_response(request, canonical(target))
                )
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=automatic_enqueuer(fixture, pointers),
                )

                class CrashAfterRun:
                    async def execute(
                        self, command: GitLabMergeRequestCommand
                    ) -> GitLabMergeRequestHandlingResult:
                        await handler.execute(command)
                        raise asyncio.CancelledError()

                crashing = receiver(fixture, CrashAfterRun(), clock)
                await crashing.execute(receipt_for(target))
                with pytest.raises(asyncio.CancelledError):
                    await crashing.replay_pending()
                assert len(pointers.messages) == 1
                clock.value += timedelta(minutes=6)
                assert await receiver(fixture, handler, clock).replay_pending() == 1
                assert len(pointers.messages) == 1
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL"
                            )
                        )
                        == 1
                    )

    asyncio.run(scenario())
