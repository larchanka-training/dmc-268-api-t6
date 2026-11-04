"""A failed Bitbucket PR read cannot strand later candidates in a push receipt."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.modules.integrations.bitbucket.application.provider_error import (
    BitbucketPullRequestFetchError,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPushContinuationPending,
    BitbucketPushRetryPending,
    ProjectBitbucketPullRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult, EnqueueStatus
from app.modules.reviews.infrastructure.bitbucket_pull_request_projection import (
    SqlAlchemyBitbucketProjectionAuthority,
    SqlAlchemyBitbucketProjectionUnitOfWork,
    SqlAlchemyBitbucketPushCandidateReader,
)
from tests.bitbucket_connection_postgres_support import _config
from tests.bitbucket_projection_support import (
    Current,
    Enqueuer,
    seed_confirmed_bitbucket_repository,
)

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
@pytest.mark.parametrize("reason", ["pull_request_missing", "pull_request_unavailable"])
def test_one_bad_pr_is_queued_while_later_push_candidates_advance(
    bitbucket_connection_schema: Connection, reason: str
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
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    delivery_id = "bitbucket-push-bad-pr-" + str(uuid4())

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                repository = await seed_confirmed_bitbucket_repository(
                    session,
                    workspace=workspace,
                    installation=installation,
                    remote_workspace=remote_workspace,
                    remote_repo=remote_repo,
                    cipher=cipher,
                )
                await session.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id,repository_id,external_id,external_number,title,source_branch,"
                        "target_branch,base_sha,head_sha,state,web_url) VALUES "
                        "(:id,:repository,:number,:number,'PR','feature','main',:base,:head,"
                        "'open','https://bitbucket.org/team/repo')"
                    ),
                    [
                        {
                            "id": uuid4(),
                            "repository": repository,
                            "number": number,
                            "base": "b" * 40,
                            "head": "a" * 40,
                        }
                        for number in range(1, 14)
                    ],
                )
                await session.execute(
                    text(
                        "INSERT INTO webhook_events "
                        "(id,provider_installation_id,provider,bitbucket_repository_id,"
                        "bitbucket_binding_generation,bitbucket_webhook_generation,"
                        "delivery_id,event,payload) VALUES "
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

            class Projector(ProjectBitbucketPullRequest):
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
                    if number == 3:
                        raise BitbucketPullRequestFetchError(reason)
                    return EnqueueResult(EnqueueStatus.ENQUEUED, uuid4())

            projector = Projector(
                authority=SqlAlchemyBitbucketProjectionAuthority(sessions),
                current=Current(),
                uow_factory=lambda: SqlAlchemyBitbucketProjectionUnitOfWork(sessions),
                push_candidates=SqlAlchemyBitbucketPushCandidateReader(sessions),
                enqueuer=Enqueuer(),
            )
            push_args = (
                installation,
                repository,
                remote_repo,
                frozenset({"feature"}),
                1,
                2,
                delivery_id,
            )
            for _ in range(2):
                with pytest.raises(BitbucketPushContinuationPending):
                    await projector.push(*push_args)
            with pytest.raises(BitbucketPushRetryPending):
                await projector.push(*push_args)
            assert projector.seen == list(range(1, 14))
            async with sessions() as session:
                position = await session.scalar(
                    text(
                        "SELECT bitbucket_push_after_pr_number FROM webhook_events "
                        "WHERE delivery_id=:delivery"
                    ),
                    {"delivery": delivery_id},
                )
                retry = (
                    await session.execute(
                        text(
                            "SELECT retry.pr_number,retry.reason,retry.attempt_count "
                            "FROM bitbucket_push_retry_candidates AS retry "
                            "JOIN webhook_events AS receipt ON receipt.id=retry.webhook_event_id "
                            "WHERE receipt.delivery_id=:delivery"
                        ),
                        {"delivery": delivery_id},
                    )
                ).one()
            assert position == 13
            assert (retry.pr_number, retry.reason, retry.attempt_count) == (3, reason, 0)
            with pytest.raises(BitbucketPushRetryPending):
                await projector.push(*push_args)
            assert projector.seen == list(range(1, 14))
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_push_retry_candidates "
                        "SET attempted_at=now()-interval '5 minutes 1 second' "
                        "WHERE pr_number=3"
                    )
                )
            with pytest.raises(BitbucketPushRetryPending):
                await projector.push(*push_args)
            assert projector.seen == [*range(1, 14), 3]
            async with sessions() as session:
                retry_count = await session.scalar(
                    text(
                        "SELECT retry.attempt_count "
                        "FROM bitbucket_push_retry_candidates AS retry "
                        "JOIN webhook_events AS receipt ON receipt.id=retry.webhook_event_id "
                        "WHERE receipt.delivery_id=:delivery AND retry.pr_number=3"
                    ),
                    {"delivery": delivery_id},
                )
            assert retry_count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
