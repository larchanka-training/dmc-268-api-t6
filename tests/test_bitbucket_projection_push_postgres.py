"""Bitbucket canonical projection is fenced by repository binding generations."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket import bitbucket_webhook_replay_resources
from app.modules.integrations.bitbucket.application.eligibility import DetermineBitbucketEligibility
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketTargetInactive,
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
from app.modules.integrations.bitbucket.infrastructure.webhook_receipts import (
    SqlAlchemyBitbucketWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestCommand,
    BitbucketPullRequestStale,
    BitbucketPushCandidatesFailed,
    BitbucketPushContinuationPending,
    BitbucketPushRetryPending,
    CanonicalBitbucketPullRequest,
    ProjectBitbucketPullRequest,
)
from app.modules.reviews.application.publish_cancellation_signals import PublishCancellationSignals
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
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionUnitOfWork,
    SqlAlchemyPullRequestRunCanceller,
)
from app.modules.reviews.infrastructure.run_delivery import SqlAlchemyRunDeliveryReader
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from tests.bitbucket_connection_postgres_support import _config
from tests.bitbucket_projection_support import NOW, Current, Enqueuer, Publisher

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
def test_push_candidates_page_all_matching_open_prs_and_fork_sources(
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
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
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
                rows = [
                    {
                        "id": uuid4(),
                        "repository": repository,
                        "number": number,
                        "branch": "fork-feature" if number == 503 else "feature",
                        "state": "open",
                    }
                    for number in range(1, 504)
                ]
                rows += [
                    {
                        "id": uuid4(),
                        "repository": repository,
                        "number": 504,
                        "branch": "other",
                        "state": "open",
                    },
                    {
                        "id": uuid4(),
                        "repository": repository,
                        "number": 505,
                        "branch": "feature",
                        "state": "closed",
                    },
                    {
                        "id": uuid4(),
                        "repository": sibling,
                        "number": 506,
                        "branch": "feature",
                        "state": "open",
                    },
                ]
                await session.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id,repository_id,external_id,external_number,title,source_branch,"
                        "target_branch,base_sha,head_sha,state,web_url) VALUES "
                        "(:id,:repository,:number,:number,'PR',:branch,'main',:base,:head,"
                        "CAST(:state AS code_change_state),'https://bitbucket.org/team/repo')"
                    ),
                    [{**row, "base": "b" * 40, "head": "a" * 40} for row in rows],
                )
            pages: list[tuple[str, tuple[object, ...], int]] = []

            def record_page(
                _connection: Any,
                cursor: Any,
                statement: str,
                parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                if (
                    "FROM code_changes" in statement
                    and "ORDER BY code_changes.external_number" in statement
                ):
                    pages.append((statement, tuple(parameters.values()), cursor.rowcount))

            event.listen(engine.sync_engine, "after_cursor_execute", record_page)
            try:
                candidates = await SqlAlchemyBitbucketPushCandidateReader(
                    sessions
                ).matching_open_pr_numbers(
                    installation,
                    repository,
                    remote_repo,
                    1,
                    2,
                    frozenset({"feature", "fork-feature"}),
                )
            finally:
                event.remove(engine.sync_engine, "after_cursor_execute", record_page)
            assert candidates == tuple(range(1, 504))
            assert len(pages) == 2
            assert [page[2] for page in pages] == [500, 3]
            assert all("LIMIT" in page[0] and 500 in page[1] for page in pages)
            assert "code_changes.external_number >" in pages[1][0]

            delivery_id = "bitbucket-push-continuation-" + str(uuid4())
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO webhook_events (id,provider_installation_id,provider,"
                        "bitbucket_repository_id,bitbucket_binding_generation,"
                        "bitbucket_webhook_generation,delivery_id,event,payload) VALUES "
                        "(:id,:installation,'bitbucket',:repository,1,2,:delivery,"
                        "'repo:push','{}'::jsonb)"
                    ),
                    {
                        "id": uuid4(),
                        "installation": installation,
                        "repository": repository,
                        "delivery": delivery_id,
                    },
                )

            class InterruptingProjector(ProjectBitbucketPullRequest):
                seen: list[int] = []
                interrupt_once = True

                async def pull_request(
                    self,
                    installation_id: UUID,
                    repository_id: UUID,
                    repository_uuid: UUID,
                    number: int,
                    binding_generation: int,
                    webhook_generation: int,
                ) -> EnqueueResult | None:
                    if number == 6 and self.interrupt_once:
                        self.interrupt_once = False
                        raise RuntimeError("interrupted after a committed page")
                    self.seen.append(number)
                    if number == 503:
                        return await super().pull_request(
                            installation_id,
                            repository_id,
                            repository_uuid,
                            number,
                            binding_generation,
                            webhook_generation,
                        )
                    return EnqueueResult(EnqueueStatus.ENQUEUED, uuid4())

            class CurrentFork:
                async def get_current(
                    self, target: BoundBitbucketTarget, number: int
                ) -> CanonicalBitbucketPullRequest:
                    assert number == 503
                    return CanonicalBitbucketPullRequest(
                        remote_repo,
                        number,
                        "open",
                        "Fork changed during continuation",
                        None,
                        "reviewer",
                        "fork-feature",
                        "main",
                        "b" * 40,
                        "c" * 40,
                        NOW + timedelta(seconds=1),
                        "https://bitbucket.org/team/repo/pull-requests/503",
                    )

            class EnqueueFork:
                calls = 0

                async def execute(
                    self,
                    code_change_id: UUID,
                    expected_head_sha: str,
                    *,
                    expected_identity: WebhookRunIdentity,
                ) -> EnqueueResult:
                    assert expected_head_sha == "c" * 40
                    self.calls += 1
                    return EnqueueResult(EnqueueStatus.ENQUEUED, uuid4())

            fork_enqueuer = EnqueueFork()

            class RefreshOnce:
                calls = 0

                async def execute(
                    self,
                    installation_id: UUID,
                    repository_id: UUID,
                    repository_uuid: UUID,
                    branches: frozenset[str],
                    binding_generation: int,
                    webhook_generation: int,
                ) -> bool:
                    self.calls += 1
                    return True

            index_refresh = RefreshOnce()

            projector = InterruptingProjector(
                authority=SqlAlchemyBitbucketProjectionAuthority(sessions),
                current=CurrentFork(),
                uow_factory=lambda: SqlAlchemyBitbucketProjectionUnitOfWork(sessions),
                push_candidates=SqlAlchemyBitbucketPushCandidateReader(sessions),
                enqueuer=fork_enqueuer,
                index_refresh=index_refresh,
            )
            push_args = (
                installation,
                repository,
                remote_repo,
                frozenset({"feature", "fork-feature"}),
                1,
                2,
                delivery_id,
            )
            with pytest.raises(BitbucketPushContinuationPending):
                await projector.push(*push_args)
            async with sessions() as session:
                position = await session.scalar(
                    text(
                        "SELECT bitbucket_push_after_pr_number FROM webhook_events "
                        "WHERE delivery_id=:delivery"
                    ),
                    {"delivery": delivery_id},
                )
            assert position == 5
            assert projector.seen == list(range(1, 6))
            with pytest.raises(RuntimeError, match="interrupted after a committed page"):
                await projector.push(*push_args)
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT bitbucket_push_after_pr_number FROM webhook_events "
                            "WHERE delivery_id=:delivery"
                        ),
                        {"delivery": delivery_id},
                    )
                    == 5
                )
            for _ in range(99):
                with pytest.raises(BitbucketPushContinuationPending):
                    await projector.push(*push_args)
            resumed = await projector.push(*push_args)
            assert len(resumed) == 3
            assert projector.seen == list(range(1, 504))
            assert projector.seen[-1] == 503  # fork-source branch
            assert fork_enqueuer.calls == 1
            assert index_refresh.calls == 1
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT head_sha FROM code_changes WHERE repository_id=:repository "
                            "AND external_number=503"
                        ),
                        {"repository": repository},
                    )
                    == "c" * 40
                )
            assert await projector.push(*push_args) == ()  # same receipt redelivered
            assert projector.seen == list(range(1, 504))

            blocked_delivery = "bitbucket-push-blocked-" + str(uuid4())
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO webhook_events (id,provider_installation_id,provider,"
                        "bitbucket_repository_id,bitbucket_binding_generation,"
                        "bitbucket_webhook_generation,delivery_id,event,payload) VALUES "
                        "(:id,:installation,'bitbucket',:repository,1,2,:delivery,"
                        "'repo:push','{}'::jsonb)"
                    ),
                    {
                        "id": uuid4(),
                        "installation": installation,
                        "repository": repository,
                        "delivery": blocked_delivery,
                    },
                )

            class DeferredProjector(ProjectBitbucketPullRequest):
                blocked = True
                seen: list[int] = []

                async def pull_request(
                    self,
                    installation_id: UUID,
                    repository_id: UUID,
                    repository_uuid: UUID,
                    number: int,
                    binding_generation: int,
                    webhook_generation: int,
                ) -> EnqueueResult | None:
                    self.seen.append(number)
                    if number == 2 and self.blocked:
                        raise BitbucketPullRequestStale()
                    return EnqueueResult(EnqueueStatus.ENQUEUED, uuid4())

            blocked_projector = DeferredProjector(
                authority=SqlAlchemyBitbucketProjectionAuthority(sessions),
                current=CurrentFork(),
                uow_factory=lambda: SqlAlchemyBitbucketProjectionUnitOfWork(sessions),
                push_candidates=SqlAlchemyBitbucketPushCandidateReader(sessions),
                enqueuer=fork_enqueuer,
            )
            blocked_args = (*push_args[:-1], blocked_delivery)
            for _ in range(101):
                with pytest.raises(BitbucketPushContinuationPending):
                    await blocked_projector.push(*blocked_args)
            assert blocked_projector.seen == list(range(1, 504))
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT bitbucket_push_after_pr_number FROM webhook_events "
                            "WHERE delivery_id=:delivery"
                        ),
                        {"delivery": blocked_delivery},
                    )
                    == 503
                )
            for _ in range(2):
                with pytest.raises(BitbucketPushRetryPending):
                    await blocked_projector.push(*blocked_args)
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM bitbucket_push_retry_candidates AS retry "
                            "JOIN webhook_events AS receipt ON receipt.id=retry.webhook_event_id "
                            "WHERE receipt.delivery_id=:delivery"
                        ),
                        {"delivery": blocked_delivery},
                    )
                    == 1
                )
            with pytest.raises(BitbucketPushCandidatesFailed):
                await blocked_projector.push(*blocked_args)
            assert blocked_projector.seen == [*range(1, 504), 2, 2, 2]
            async with sessions() as session:
                terminal = (
                    await session.execute(
                        text(
                            "SELECT retry.reason,retry.attempt_count,retry.failed_at "
                            "FROM bitbucket_push_retry_candidates AS retry "
                            "JOIN webhook_events AS receipt ON receipt.id=retry.webhook_event_id "
                            "WHERE receipt.delivery_id=:delivery"
                        ),
                        {"delivery": blocked_delivery},
                    )
                ).one()
                assert terminal.reason == "canonical_stale"
                assert terminal.attempt_count == 3
                assert terminal.failed_at is not None
            # Settlement committed the terminal candidate, but the worker died
            # before recording receipt failure. A later matching PR extends the scan.
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id,repository_id,external_id,external_number,title,source_branch,"
                        "target_branch,base_sha,head_sha,state,web_url) VALUES "
                        "(:id,:repository,507,507,'PR','feature','main',:base,:head,"
                        "'open','https://bitbucket.org/team/repo')"
                    ),
                    {
                        "id": uuid4(),
                        "repository": repository,
                        "base": "b" * 40,
                        "head": "a" * 40,
                    },
                )
            with pytest.raises(BitbucketPushCandidatesFailed):
                await blocked_projector.push(*blocked_args)
            assert blocked_projector.seen[-1] == 507
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT bitbucket_push_after_pr_number FROM webhook_events "
                            "WHERE delivery_id=:delivery"
                        ),
                        {"delivery": blocked_delivery},
                    )
                    == 507
                )
            terminal_claim = uuid4()
            terminal_now = datetime.now(UTC)
            async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.claim(
                        blocked_delivery,
                        terminal_claim,
                        terminal_now,
                        terminal_now + timedelta(minutes=5),
                    )
                    is not None
                )
                await uow.commit()
            async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(sessions) as uow:
                await uow.receipts.fail_push_candidates(
                    blocked_delivery, terminal_claim, terminal_now
                )
                await uow.commit()
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT projection_failed_at FROM webhook_events "
                            "WHERE delivery_id=:delivery"
                        ),
                        {"delivery": blocked_delivery},
                    )
                    == terminal_now
                )

            with pytest.raises(BitbucketBindingGenerationChanged):
                await SqlAlchemyBitbucketPushCandidateReader(sessions).matching_open_pr_numbers(
                    installation, repository, remote_repo, 2, 2, frozenset({"feature"})
                )

            pages_before_rebind = 0

            def rebind_after_first_page(
                connection: Connection,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                nonlocal pages_before_rebind
                if "ORDER BY code_changes.external_number" not in statement:
                    return
                pages_before_rebind += 1
                if pages_before_rebind == 1:
                    connection.execute(
                        text(
                            "UPDATE bitbucket_repository_webhook_secrets "
                            "SET binding_generation=binding_generation+1 WHERE repository_id=:id"
                        ),
                        {"id": repository},
                    )

            event.listen(engine.sync_engine, "after_cursor_execute", rebind_after_first_page)
            try:
                with pytest.raises(BitbucketBindingGenerationChanged):
                    await SqlAlchemyBitbucketPushCandidateReader(sessions).matching_open_pr_numbers(
                        installation,
                        repository,
                        remote_repo,
                        1,
                        2,
                        frozenset({"feature", "fork-feature"}),
                    )
                assert pages_before_rebind == 1
            finally:
                event.remove(engine.sync_engine, "after_cursor_execute", rebind_after_first_page)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_repository_webhook_secrets "
                        "SET binding_generation=binding_generation+1 WHERE repository_id=:id"
                    ),
                    {"id": repository},
                )
            with pytest.raises(BitbucketBindingGenerationChanged):
                await projector.push(*push_args)
            assert projector.seen == list(range(1, 504))
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    with pytest.raises(RuntimeError, match="Cannot downgrade 20261103_0056"):
        command.downgrade(_config(db), "20261103_0055")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("close_event", "remote_closed_state", "projected_state"),
    [
        ("pullrequest:fulfilled", "MERGED", "merged"),
        ("pullrequest:rejected", "DECLINED", "closed"),
    ],
)
def test_projection_current_head_metadata_push_and_disconnect(
    bitbucket_connection_schema: Connection,
    close_event: str,
    remote_closed_state: str,
    projected_state: str,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo, sibling_remote, source_fork = (
        uuid4() for _ in range(6)
    )
    source_workspace = uuid4()
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
            for column in ("enabled", "installation_available"):
                async with sessions.begin() as session:
                    await session.execute(
                        text(f"UPDATE repositories SET {column}=false WHERE id=:id"),
                        {"id": repository},
                    )
                with pytest.raises(BitbucketTargetInactive):
                    await projector.execute(command)
                assert (
                    await SqlAlchemyBitbucketEligibilityCandidateStore(sessions).get(
                        first.projection.code_change_id
                    )
                    is None
                )
                assert enqueuer.heads == ["a" * 40]
                async with sessions.begin() as session:
                    await session.execute(
                        text(f"UPDATE repositories SET {column}=true WHERE id=:id"),
                        {"id": repository},
                    )
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
            remote_state = ["OPEN"]
            fork_reads: list[str] = []
            closed_base_reads: list[str] = []
            pr_commit_reads: list[str] = []

            def respond(request: httpx.Request) -> httpx.Response:
                assert request.url.host == "api.bitbucket.org"
                assert request.headers["Authorization"] == "Bearer test-token"
                if f"{{{source_fork}}}" in request.url.path:
                    fork_reads.append(str(request.url))
                    return httpx.Response(404)
                if request.url.path.endswith("/pullrequests/17/commits"):
                    pr_commit_reads.append(str(request.url))
                    assert dict(request.url.params) == {"pagelen": "1"}
                    return httpx.Response(
                        200,
                        json={
                            "values": [{"hash": "d" * 40}],
                            "next": str(request.url) + "&page=67Fg",
                        },
                    )
                if request.url.path.endswith(f"/commit/{remote_head[0][:12]}"):
                    if remote_head[0] == "d" * 40:
                        return httpx.Response(404)
                    return httpx.Response(200, json={"hash": remote_head[0]})
                if request.url.path.endswith("/commit/bbbbbbbbbbbb"):
                    if remote_state[0] != "OPEN":
                        closed_base_reads.append(str(request.url))
                        return httpx.Response(404)
                    return httpx.Response(200, json={"hash": "b" * 40})
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
                        "state": remote_state[0],
                        "title": "Webhook current",
                        "updated_on": (
                            "2026-10-26T12:00:03+00:00"
                            if remote_head[0] == "d" * 40
                            else "2026-10-26T12:00:01+00:00"
                            if remote_head[0] == "a" * 40
                            else "2026-10-26T12:00:06+00:00"
                            if remote_state[0] != "OPEN"
                            else "2026-10-26T12:00:04+00:00"
                        ),
                        "author": {"nickname": "reviewer"},
                        "source": {
                            "branch": {"name": "feature"},
                            "commit": (
                                {"hash": remote_head[0][:12]} if remote_state[0] == "OPEN" else None
                            ),
                            "repository": (
                                {
                                    "uuid": "{" + str(source_fork) + "}",
                                    "workspace": {"uuid": "{" + str(source_workspace) + "}"},
                                }
                                if remote_state[0] == "OPEN"
                                else None
                            ),
                        },
                        "destination": {
                            "branch": {"name": "main"},
                            "commit": {"hash": "b" * 12},
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
                assert fork_reads == []
                assert len(pr_commit_reads) == 1
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
                redelivery = prepare_bitbucket_receipt(
                    origin=origin,
                    event_name="repo:push",
                    hook_uuid=uuid4(),
                    request_uuid=uuid4(),
                    payload_json=push.payload_json,
                )
                assert (
                    await replay.receiver.execute(redelivery)
                    == InstallationDeliveryDispatchStatus.PENDING
                )
                redelivery_sweep = await replay.receiver.replay_batch()
                assert (redelivery_sweep.handled_count, redelivery_sweep.error_count) == (1, 0)
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM runs WHERE code_change_id IN "
                                "(SELECT id FROM code_changes WHERE repository_id=:repository) "
                                "AND head_sha=:head"
                            ),
                            {"repository": repository, "head": "e" * 40},
                        )
                        == 1
                    )
                async with sessions.begin() as session:
                    await session.execute(
                        text("UPDATE runs SET attempt=1 WHERE id=:id"),
                        {"id": publisher.messages[-1].run_id},
                    )
                remote_state[0] = remote_closed_state
                closed = prepare_bitbucket_receipt(
                    origin=origin,
                    event_name=close_event,
                    hook_uuid=uuid4(),
                    request_uuid=uuid4(),
                    payload_json=(
                        '{"repository":{"uuid":"{' + str(remote_repo) + '}"},'
                        '"pullrequest":{"id":17}}'
                    ),
                )
                assert (
                    await replay.receiver.execute(closed)
                    == InstallationDeliveryDispatchStatus.PENDING
                )
                closed_sweep = await replay.receiver.replay_batch()
                assert (closed_sweep.handled_count, closed_sweep.error_count) == (1, 0)
                assert closed_base_reads == []
                assert len(publisher.messages) == 3
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT state FROM code_changes WHERE repository_id=:id"),
                            {"id": repository},
                        )
                        == projected_state
                    )
                    assert (
                        await session.scalar(
                            text("SELECT head_sha FROM code_changes WHERE repository_id=:id"),
                            {"id": repository},
                        )
                        == "e" * 40
                    )
                    cancelled = (
                        await session.execute(
                            text(
                                "SELECT state,error_code,cancellation_signal_requested_at "
                                "FROM runs WHERE id=:id"
                            ),
                            {"id": publisher.messages[-1].run_id},
                        )
                    ).one()
                    assert cancelled.state == "cancelled"
                    assert cancelled.error_code == "closed"
                    assert cancelled.cancellation_signal_requested_at is not None
                    signals = await SqlAlchemyPullRequestRunCanceller(
                        session
                    ).pending_cancellation_signals(10)
                    assert [message.run_id for message in signals] == [
                        publisher.messages[-1].run_id
                    ]
                cancellation_messages: list[PendingRunMessage] = []

                class CancellationPublisher:
                    async def publish_confirmed(
                        self,
                        message: PendingRunMessage,
                        *,
                        kind: RunPublicationKind = RunPublicationKind.QUEUED,
                    ) -> None:
                        assert kind == RunPublicationKind.CANCELLATION
                        cancellation_messages.append(message)

                published = await PublishCancellationSignals(
                    uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(sessions),
                    publisher=CancellationPublisher(),
                    now=lambda: NOW,
                ).replay_pending()
                assert published == 1
                assert [message.run_id for message in cancellation_messages] == [
                    publisher.messages[-1].run_id
                ]
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT cancellation_signal_published_at FROM runs WHERE id=:id"),
                            {"id": publisher.messages[-1].run_id},
                        )
                        == NOW
                    )
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
