"""Public GitLab projection/receipt flows on real PostgreSQL and synthetic HTTP."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
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
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveWebhookDelivery,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)
from app.modules.reviews.application.project_gitlab_merge_request import (
    GitLabMergeRequestCommand,
    GitLabMergeRequestHandler,
    ProjectGitLabMergeRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
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
