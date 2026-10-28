"""Publication claim transactions are owned by the use case."""

from __future__ import annotations

import asyncio
import os
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.modules.reviews.application.bitbucket_publication_claims import (
    BitbucketPublicationClaims,
    PublicationClaim,
)
from app.modules.reviews.application.publish_run_review import ObsoletePublication
from app.modules.reviews.infrastructure.bitbucket_publication_claims import (
    SqlAlchemyBitbucketPublicationClaimUnitOfWork,
)
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)


class FakeClaimUnitOfWork:
    def __init__(self) -> None:
        self.active = False
        self.commits = 0
        self.actions: list[str] = []

    @property
    def claims(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        assert not self.active
        self.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False

    async def commit(self) -> None:
        assert self.active
        self.commits += 1

    async def rollback(self) -> None:
        assert self.active

    async def acquire(self, run_id: UUID) -> PublicationClaim:
        assert self.active
        self.actions.append("acquire")
        return PublicationClaim(run_id, UUID(int=1), 1)

    async def renew(self, claim: PublicationClaim) -> None:
        assert self.active
        self.actions.append("renew")

    async def release(self, claim: PublicationClaim) -> None:
        assert self.active
        self.actions.append("release")


def test_publication_claim_use_case_commits_each_short_transaction() -> None:
    uow = FakeClaimUnitOfWork()
    claims = BitbucketPublicationClaims(lambda: uow)
    run_id = uuid4()

    async def scenario() -> None:
        claim = await claims.acquire(run_id)
        assert not uow.active
        await claims.renew(claim)
        assert not uow.active
        await claims.release(claim)
        assert not uow.active

    asyncio.run(scenario())
    assert uow.actions == ["acquire", "renew", "release"]
    assert uow.commits == 3


@pytest.mark.integration
def test_publication_claim_rollback_and_stale_owner_fencing(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, repository, change, rule, prompt, run_id = (uuid4() for _ in range(7))
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    db.execute(
        text(
            "INSERT INTO provider_installations "
            "(id,workspace_id,provider,external_id,metadata) "
            "VALUES (:id,:workspace,'bitbucket',123,'{}'::jsonb)"
        ),
        {"id": installation, "workspace": workspace},
    )
    db.execute(
        text(
            "INSERT INTO repositories "
            "(id,provider_installation_id,external_id,full_name,default_branch,web_url) "
            "VALUES (:id,:installation,123,'team/repo','main','https://bitbucket.org/team/repo')"
        ),
        {"id": repository, "installation": installation},
    )
    db.execute(
        text(
            "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
            "VALUES (:id,'review.system',1,'prompt',:checksum,true)"
        ),
        {"id": prompt, "checksum": "c" * 64},
    )
    db.execute(
        text(
            "INSERT INTO rule_versions (id,repository_id,version,rules,checksum,is_active) "
            "VALUES (:id,:repo,1,'[]'::jsonb,:checksum,true)"
        ),
        {"id": rule, "repo": repository, "checksum": "d" * 64},
    )
    db.execute(
        text(
            "INSERT INTO code_changes "
            "(id,repository_id,external_id,external_number,title,source_branch,"
            "target_branch,base_sha,head_sha,state,web_url) VALUES "
            "(:id,:repo,7,7,'PR','feature/review','main',:base,:head,'open',:url)"
        ),
        {
            "id": change,
            "repo": repository,
            "base": "b" * 40,
            "head": "a" * 40,
            "url": "https://bitbucket.org/team/repo/pull-requests/7",
        },
    )
    db.execute(
        text(
            "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
            "base_sha,base_ref,head_sha,state,trigger,idempotency_key,engine,"
            "rule_version_id,prompt_version_id,available_at) VALUES "
            "(:id,:change,:installation,:base,'main',:head,'queued','manual',"
            ":key,'fast',:rule,:prompt,now())"
        ),
        {
            "id": run_id,
            "change": change,
            "installation": installation,
            "base": "b" * 40,
            "head": "a" * 40,
            "key": "f" * 64,
            "rule": rule,
            "prompt": prompt,
        },
    )
    db.commit()
    schema = str(db.scalar(text("SELECT current_schema()")))
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            claims = BitbucketPublicationClaims(
                lambda: SqlAlchemyBitbucketPublicationClaimUnitOfWork(sessions)
            )
            async with SqlAlchemyBitbucketPublicationClaimUnitOfWork(sessions) as uow:
                uncommitted = await uow.claims.acquire(run_id)
                assert uncommitted.generation == 1
            first = await claims.acquire(run_id)
            assert first.generation == 1
            with pytest.raises(ObsoletePublication):
                await claims.acquire(run_id)
            await claims.renew(first)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_publication_claims "
                        "SET lease_until=now()-interval '1 second' WHERE run_id=:run"
                    ),
                    {"run": run_id},
                )
            second = await claims.acquire(run_id)
            assert second.generation == 2
            with pytest.raises(ObsoletePublication):
                await claims.renew(first)
            await claims.release(first)
            await claims.renew(second)
            await claims.release(second)
            async with sessions() as session:
                remaining = await session.scalar(
                    text("SELECT count(*) FROM bitbucket_publication_claims WHERE run_id=:run"),
                    {"run": run_id},
                )
            assert remaining == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())
