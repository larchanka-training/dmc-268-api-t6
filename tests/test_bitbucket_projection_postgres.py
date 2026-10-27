"""Bitbucket canonical projection is fenced by repository binding generations."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket import bitbucket_webhook_replay_resources
from app.modules.integrations.bitbucket.application.eligibility import DetermineBitbucketEligibility
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.application.webhook_receipts import (
    AuthenticatedBitbucketOrigin,
    prepare_bitbucket_receipt,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestCommand,
    BitbucketRunEnqueuer,
    CanonicalBitbucketPullRequest,
    ProjectBitbucketPullRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
    PendingRunMessage,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.bitbucket_eligibility_candidates import (
    SqlAlchemyBitbucketEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.bitbucket_pull_request_projection import (
    SqlAlchemyBitbucketProjectionAuthority,
    SqlAlchemyBitbucketProjectionUnitOfWork,
    SqlAlchemyBitbucketPushCandidateReader,
)
from app.modules.reviews.infrastructure.run_delivery import SqlAlchemyRunDeliveryReader
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


@dataclass
class Current:
    head: str = "a" * 40
    updated_at: datetime = NOW
    title: str = "First"

    async def get_current(
        self, target: BoundBitbucketTarget, number: int
    ) -> CanonicalBitbucketPullRequest:
        return CanonicalBitbucketPullRequest(
            target.repository_uuid,
            number,
            "open",
            self.title,
            None,
            "reviewer",
            "feature",
            "main",
            "b" * 40,
            self.head,
            self.updated_at,
            f"https://bitbucket.org/team/repo/pull-requests/{number}",
        )


@dataclass
class Enqueuer:
    heads: list[str] = field(default_factory=list)

    async def execute(
        self, code_change_id: UUID, expected_head_sha: str, *, expected_identity: WebhookRunIdentity
    ) -> EnqueueResult:
        assert expected_identity.provider == "bitbucket"
        self.heads.append(expected_head_sha)
        return EnqueueResult(EnqueueStatus.ENQUEUED, uuid4())


@dataclass
class Publisher:
    messages: list[PendingRunMessage] = field(default_factory=list)
    fail_heads: set[str] = field(default_factory=set)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert message.provider == "bitbucket" and kind == RunPublicationKind.QUEUED
        self.messages.append(message)
        if message.head_sha in self.fail_heads:
            raise RuntimeError("synthetic publisher failure")


class FailingEnqueuer:
    async def execute(
        self, code_change_id: UUID, expected_head_sha: str, *, expected_identity: WebhookRunIdentity
    ) -> EnqueueResult:
        raise RuntimeError("synthetic crash after PR projection")


@pytest.mark.integration
def test_same_head_replay_recovers_missing_and_deferred_run(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo = (uuid4() for _ in range(4))
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace,
                    installation_id=installation,
                    bitbucket_workspace_uuid=remote_workspace,
                    token=cipher.encrypt(
                        "test-token",
                        context=CredentialContext(installation, workspace),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                repository, _ = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_uuid=remote_repo,
                    full_name="team/repo",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/repo",
                )
                await store.set_repository_webhook_secret(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_id=repository,
                    secret=cipher.encrypt(
                        "s" * 40,
                        context=CredentialContext(installation, workspace, repository),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_repository_webhook_secrets SET confirmed_at=now() "
                        "WHERE repository_id=:id"
                    ),
                    {"id": repository},
                )
                await session.execute(
                    text(
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.system',1,'prompt',:checksum,true)"
                    ),
                    {"id": uuid4(), "checksum": "p" * 64},
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id,repository_id,version,rules,checksum,is_active) "
                        "VALUES (:id,:repository,1,'[]'::jsonb,:checksum,true)"
                    ),
                    {"id": uuid4(), "repository": repository, "checksum": "a" * 64},
                )
            authority = SqlAlchemyBitbucketProjectionAuthority(sessions)
            current = Current()
            command = BitbucketPullRequestCommand(installation, repository, remote_repo, 17, 1, 2)

            def projector(enqueuer: BitbucketRunEnqueuer) -> ProjectBitbucketPullRequest:
                return ProjectBitbucketPullRequest(
                    authority=authority,
                    current=current,
                    uow_factory=lambda: SqlAlchemyBitbucketProjectionUnitOfWork(sessions),
                    push_candidates=SqlAlchemyBitbucketPushCandidateReader(sessions),
                    enqueuer=enqueuer,
                )

            with pytest.raises(RuntimeError, match="synthetic crash"):
                await projector(FailingEnqueuer()).execute(command)
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 1
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
            publisher = Publisher()
            actual = TryEnqueueWebhookRun(
                eligibility=DetermineBitbucketEligibility(
                    candidates=SqlAlchemyBitbucketEligibilityCandidateStore(sessions)
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(sessions),
                publisher=publisher,
                now=lambda: NOW,
            )
            running = projector(actual)
            replayed = await running.execute(command)
            assert not replayed.projection.head_changed
            assert replayed.enqueue is not None
            assert replayed.enqueue.status == EnqueueStatus.ENQUEUED
            assert len(publisher.messages) == 1
            first_run = publisher.messages[0]
            current.title = "Metadata only"
            current.updated_at = NOW + timedelta(seconds=1)
            metadata = await running.execute(command)
            assert not metadata.projection.head_changed and metadata.enqueue is None
            assert len(publisher.messages) == 1
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='running',attempt=1 WHERE id=:id"),
                    {"id": first_run.run_id},
                )
            current.head = "b" * 40
            current.updated_at = NOW + timedelta(seconds=2)
            blocked = await running.execute(command)
            assert blocked.projection.head_changed
            assert blocked.enqueue is not None
            assert blocked.enqueue.status == EnqueueStatus.DEFERRED
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='cancelled' WHERE id=:id"),
                    {"id": first_run.run_id},
                )
            retried = await running.execute(command)
            assert not retried.projection.head_changed
            assert retried.enqueue is not None
            assert retried.enqueue.status == EnqueueStatus.ENQUEUED
            assert len(publisher.messages) == 2
            duplicate = await running.execute(command)
            assert not duplicate.projection.head_changed and duplicate.enqueue is None
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_projection_current_head_metadata_push_and_disconnect(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo, sibling_remote = (
        uuid4() for _ in range(5)
    )
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace,
                    installation_id=installation,
                    bitbucket_workspace_uuid=remote_workspace,
                    token=cipher.encrypt(
                        "test-token",
                        context=CredentialContext(installation, workspace),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                repository, _ = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_uuid=remote_repo,
                    full_name="team/repo",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/repo",
                )
                await store.set_repository_webhook_secret(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_id=repository,
                    secret=cipher.encrypt(
                        "s" * 40,
                        context=CredentialContext(installation, workspace, repository),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_repository_webhook_secrets SET confirmed_at=now() "
                        "WHERE repository_id=:id"
                    ),
                    {"id": repository},
                )
                sibling, _ = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_uuid=sibling_remote,
                    full_name="team/sibling",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/sibling",
                )
                await store.set_repository_webhook_secret(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_id=sibling,
                    secret=cipher.encrypt(
                        "s" * 40,
                        context=CredentialContext(installation, workspace, sibling),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_repository_webhook_secrets SET confirmed_at=now() "
                        "WHERE repository_id=:id"
                    ),
                    {"id": sibling},
                )
            authority = SqlAlchemyBitbucketProjectionAuthority(sessions)
            current = Current()
            enqueuer = Enqueuer()
            projector = ProjectBitbucketPullRequest(
                authority=authority,
                current=current,
                uow_factory=lambda: SqlAlchemyBitbucketProjectionUnitOfWork(sessions),
                push_candidates=SqlAlchemyBitbucketPushCandidateReader(sessions),
                enqueuer=enqueuer,
            )
            command = BitbucketPullRequestCommand(
                installation,
                repository,
                remote_repo,
                17,
                1,
                2,
            )
            first = await projector.execute(command)
            assert first.projection.head_changed
            assert enqueuer.heads == ["a" * 40]
            current.title = "Edited metadata"
            current.updated_at = NOW + timedelta(seconds=1)
            metadata = await projector.execute(command)
            assert not metadata.projection.head_changed
            # The fake enqueuer persisted no Run, so same-head retry is required.
            assert enqueuer.heads == ["a" * 40, "a" * 40]
            candidates = await SqlAlchemyBitbucketPushCandidateReader(
                sessions
            ).matching_open_pr_numbers(
                installation, repository, remote_repo, 1, 2, frozenset({"feature"})
            )
            assert candidates == (17,)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.system',1,'prompt',:checksum,true)"
                    ),
                    {"id": uuid4(), "checksum": "p" * 64},
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id,repository_id,version,rules,checksum,is_active) "
                        "VALUES (:id,:repository,1,'[]'::jsonb,:checksum,true)"
                    ),
                    {"id": uuid4(), "repository": repository, "checksum": "a" * 64},
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id,repository_id,version,rules,checksum,is_active) "
                        "VALUES (:id,:repository,1,'[]'::jsonb,:checksum,true)"
                    ),
                    {"id": uuid4(), "repository": sibling, "checksum": "b" * 64},
                )
            publisher = Publisher()
            actual = TryEnqueueWebhookRun(
                eligibility=DetermineBitbucketEligibility(
                    candidates=SqlAlchemyBitbucketEligibilityCandidateStore(sessions)
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(sessions),
                publisher=publisher,
                now=lambda: NOW,
            )
            running_projector = ProjectBitbucketPullRequest(
                authority=authority,
                current=current,
                uow_factory=lambda: SqlAlchemyBitbucketProjectionUnitOfWork(sessions),
                push_candidates=SqlAlchemyBitbucketPushCandidateReader(sessions),
                enqueuer=actual,
            )
            current.head = "c" * 40
            current.updated_at = NOW + timedelta(seconds=2)
            changed = await running_projector.execute(command)
            assert changed.projection.head_changed
            assert enqueuer.heads == ["a" * 40, "a" * 40]
            assert changed.enqueue is not None and changed.enqueue.status == EnqueueStatus.ENQUEUED
            assert len(publisher.messages) == 1
            run = publisher.messages[0]
            async with sessions() as session:
                origin_row = (
                    await session.execute(
                        text(
                            "SELECT workspace_id,provider_installation_id,repository_id,"
                            "repository_uuid,binding_generation,credential_generation,"
                            "pr_number,head_sha FROM bitbucket_run_origins WHERE run_id=:id"
                        ),
                        {"id": run.run_id},
                    )
                ).one()
                assert tuple(origin_row) == (
                    workspace,
                    installation,
                    repository,
                    remote_repo,
                    1,
                    1,
                    17,
                    "c" * 40,
                )
            assert await SqlAlchemyRunDeliveryReader(sessions).read(run.run_id) is not None
            remote_head = ["d" * 40]

            def respond(request: httpx.Request) -> httpx.Response:
                assert request.url.host == "api.bitbucket.org"
                assert request.headers["Authorization"] == "Bearer test-token"
                if "pullrequests" not in request.url.path:
                    return httpx.Response(
                        200,
                        json={
                            "uuid": "{" + str(remote_repo) + "}",
                            "workspace": {
                                "uuid": "{" + str(remote_workspace) + "}",
                                "slug": "team",
                            },
                            "slug": "repo",
                            "full_name": "team/repo",
                            "name": "Repo",
                            "is_private": True,
                            "mainbranch": {"name": "main"},
                            "links": {"html": {"href": "https://bitbucket.org/team/repo"}},
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "id": 17,
                        "state": "OPEN",
                        "title": "Webhook current",
                        "updated_on": (
                            "2026-10-26T12:00:03+00:00"
                            if remote_head[0] == "d" * 40
                            else "2026-10-26T12:00:01+00:00"
                            if remote_head[0] == "a" * 40
                            else "2026-10-26T12:00:04+00:00"
                        ),
                        "author": {"nickname": "reviewer"},
                        "source": {
                            "branch": {"name": "feature"},
                            "commit": {"hash": remote_head[0]},
                        },
                        "destination": {
                            "branch": {"name": "main"},
                            "commit": {"hash": "b" * 40},
                            "repository": {"uuid": "{" + str(remote_repo) + "}"},
                        },
                    },
                )

            origin = AuthenticatedBitbucketOrigin(
                installation, repository, workspace, remote_repo, 1, 2
            )
            async with bitbucket_webhook_replay_resources(
                sessions,
                cipher=cipher,
                run_publisher=publisher,
                transport=httpx.MockTransport(respond),
            ) as replay:
                delivery = prepare_bitbucket_receipt(
                    origin=origin,
                    event_name="pullrequest:updated",
                    hook_uuid=uuid4(),
                    request_uuid=uuid4(),
                    payload_json=(
                        '{"repository":{"uuid":"{' + str(remote_repo) + '}"},'
                        '"pullrequest":{"id":17}}'
                    ),
                )
                assert (
                    await replay.receiver.execute(delivery)
                    == InstallationDeliveryDispatchStatus.PENDING
                )
                swept = await replay.receiver.replay_batch()
                assert (swept.handled_count, swept.error_count) == (1, 0)
                assert [item.head_sha for item in publisher.messages] == ["c" * 40, "d" * 40]
                assert (
                    await replay.receiver.execute(delivery)
                    == InstallationDeliveryDispatchStatus.DUPLICATE
                )
                assert (await replay.receiver.replay_batch()).handled_count == 0
                remote_head[0] = "a" * 40
                out_of_order = prepare_bitbucket_receipt(
                    origin=origin,
                    event_name="pullrequest:updated",
                    hook_uuid=uuid4(),
                    request_uuid=uuid4(),
                    payload_json=(
                        '{"repository":{"uuid":"{' + str(remote_repo) + '}"},'
                        '"pullrequest":{"id":17,"source":{"commit":{"hash":"' + "a" * 40 + '"}}}}'
                    ),
                )
                assert (
                    await replay.receiver.execute(out_of_order)
                    == InstallationDeliveryDispatchStatus.PENDING
                )
                sweep = await replay.receiver.replay_batch()
                assert (sweep.handled_count, sweep.error_count) == (1, 0)
                assert [item.head_sha for item in publisher.messages] == ["c" * 40, "d" * 40]
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT head_sha FROM code_changes WHERE repository_id=:id"),
                            {"id": repository},
                        )
                        == "d" * 40
                    )
                remote_head[0] = "e" * 40
                publisher.fail_heads.add("e" * 40)
                push = prepare_bitbucket_receipt(
                    origin=origin,
                    event_name="repo:push",
                    hook_uuid=uuid4(),
                    request_uuid=uuid4(),
                    payload_json=(
                        '{"repository":{"uuid":"{' + str(remote_repo) + '}"},'
                        '"push":{"changes":[{"new":{"type":"branch",'
                        '"name":"feature","target":{"hash":"' + "e" * 40 + '"}}}]}}'
                    ),
                )
                assert (
                    await replay.receiver.execute(push)
                    == InstallationDeliveryDispatchStatus.PENDING
                )
                swept = await replay.receiver.replay_batch()
                assert (swept.handled_count, swept.error_count) == (1, 0)
                assert [item.head_sha for item in publisher.messages] == [
                    "c" * 40,
                    "d" * 40,
                    "e" * 40,
                ]
            stale_pending = publisher.messages[-1]
            current.head = "f" * 40
            current.updated_at = NOW + timedelta(seconds=5)
            sibling_command = BitbucketPullRequestCommand(
                installation, sibling, sibling_remote, 18, 1, 2
            )
            sibling_outcome = await running_projector.execute(sibling_command)
            assert sibling_outcome.enqueue is not None
            sibling_run = publisher.messages[-1]
            assert await SqlAlchemyRunDeliveryReader(sessions).read(sibling_run.run_id) is not None
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT title,head_sha,provider_updated_at "
                            "FROM code_changes WHERE repository_id=:id"
                        ),
                        {"id": repository},
                    )
                ).one()
                assert (row.title, row.head_sha) == ("Webhook current", "e" * 40)
                assert (
                    await session.scalar(
                        text("SELECT state FROM runs WHERE id=:id"), {"id": run.run_id}
                    )
                    == "cancelled"
                )
            async with sessions.begin() as session:
                await SqlAlchemyBitbucketConnectionStore(session).disconnect_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_id=repository,
                )
            assert await SqlAlchemyRunDeliveryReader(sessions).read(run.run_id) is None
            assert await SqlAlchemyRunDeliveryReader(sessions).read(stale_pending.run_id) is None
            assert await SqlAlchemyRunDeliveryReader(sessions).read(sibling_run.run_id) is not None
            run_inputs = SqlAlchemyRunRepository(sessions, allow_unscoped=True)
            assert await run_inputs.get_run_vcs_input(stale_pending.run_id) is None
            sibling_vcs = await run_inputs.get_run_vcs_input(sibling_run.run_id)
            assert sibling_vcs is not None
            assert sibling_vcs.locator.repository_uuid == sibling_remote
            async with sessions.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                rebound, reactivated = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_uuid=remote_repo,
                    full_name="team/repo",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/repo",
                )
                assert rebound == repository and reactivated
                await store.set_repository_webhook_secret(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_id=repository,
                    secret=cipher.encrypt(
                        "t" * 40,
                        context=CredentialContext(installation, workspace, repository),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_repository_webhook_secrets SET confirmed_at=now() "
                        "WHERE repository_id=:id"
                    ),
                    {"id": repository},
                )
            attempts_before = len(publisher.messages)
            assert await actual.replay_pending_publications() == 0
            assert len(publisher.messages) == attempts_before
            assert await SqlAlchemyRunDeliveryReader(sessions).read(stale_pending.run_id) is None
            assert await SqlAlchemyRunDeliveryReader(sessions).read(sibling_run.run_id) is not None
            async with sessions.begin() as session:
                lifecycle = SqlAlchemyRunLifecycleStore(session)
                stale_claim = await lifecycle.lock_for_claim(stale_pending.run_id)
                sibling_claim = await lifecycle.lock_for_claim(sibling_run.run_id)
                assert stale_claim is not None and not stale_claim.origin_active
                assert sibling_claim is not None and sibling_claim.origin_active
            with pytest.raises(BitbucketBindingGenerationChanged):
                await projector.execute(command)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
