"""Bitbucket canonical projection is fenced by repository binding generations."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.modules.integrations.bitbucket.application.eligibility import DetermineBitbucketEligibility
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestCommand,
    BitbucketPullRequestUntrackedClosed,
    BitbucketRunEnqueuer,
    ProjectBitbucketPullRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueStatus,
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
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from tests.bitbucket_connection_postgres_support import _config
from tests.bitbucket_projection_support import NOW, Current, FailingEnqueuer, Publisher

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


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
            resolved = await authority.resolve(command)
            assert resolved.workspace_id == workspace
            assert resolved.bitbucket_workspace_uuid == remote_workspace
            assert resolved.workspace_id != resolved.bitbucket_workspace_uuid
            untracked_closed = replace(
                await current.get_current(resolved, 18),
                state="closed",
                head_sha=None,
                base_sha="b" * 12,
            )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                with pytest.raises(BitbucketPullRequestUntrackedClosed):
                    await uow.pull_requests.project(resolved, 2, untracked_closed)
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0

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
