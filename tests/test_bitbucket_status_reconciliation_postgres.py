"""Persisted status replay survives a missed terminal callback and old heads."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.reviews.application.bitbucket_status import BitbucketStatusSnapshot
from app.modules.reviews.application.bitbucket_status_intents import BitbucketStatusIntents
from app.modules.reviews.infrastructure.bitbucket_ci import (
    BitbucketBuildStatusPublisher,
    BitbucketStatusReconciler,
)
from app.modules.reviews.infrastructure.bitbucket_status_ledger import (
    SqlAlchemyBitbucketStatusUnitOfWork,
)
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)
pytestmark = pytest.mark.integration


def test_missed_terminal_status_is_recovered_and_stale_rows_do_not_starve_live(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, repository, change, rule, prompt, live_run = (
        uuid4() for _ in range(7)
    )
    stale_runs = [uuid4() for _ in range(100)]
    remote_repo = uuid4()
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
    for run_id, key, state in (
        *((run, f"{i + 1:064x}", "succeeded") for i, run in enumerate(stale_runs)),
        (live_run, "f" * 64, "succeeded"),
    ):
        db.execute(
            text(
                "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
                "base_sha,base_ref,head_sha,state,trigger,idempotency_key,engine,"
                "rule_version_id,prompt_version_id,available_at,finished_at) VALUES "
                "(:id,:change,:installation,:base,'main',:head,:state,'manual',"
                ":key,'fast',:rule,:prompt,now(),now())"
            ),
            {
                "id": run_id,
                "change": change,
                "installation": installation,
                "base": "b" * 40,
                "head": "a" * 40,
                "state": state,
                "key": key,
                "rule": rule,
                "prompt": prompt,
            },
        )
    for i in range(100):
        db.execute(
            text(
                "INSERT INTO bitbucket_status_publications "
                "(workspace_id,repository_uuid,pr_number,head_sha,run_id,run_order,"
                "run_url,desired_state,desired_rank,applied_run_id,applied_state,next_check_at) "
                "VALUES (:workspace,:repo,:pr,:head,:run,1,:url,'succeeded',3,"
                ":run,'succeeded',now()-interval '2 minutes')"
            ),
            {
                "workspace": workspace,
                "repo": remote_repo,
                "pr": i + 1,
                "head": f"{i:040x}",
                "run": stale_runs[i],
                "url": "https://review.example/stale",
            },
        )
    live_url = "https://review.example/live"
    db.execute(
        text(
            "INSERT INTO bitbucket_status_publications "
            "(workspace_id,repository_uuid,pr_number,head_sha,run_id,run_order,"
            "run_url,desired_state,desired_rank,applied_run_id,applied_state,next_check_at) "
            "VALUES (:workspace,:repo,7,:head,:run,2,:url,'running',1,"
            ":run,'running',now()-interval '1 minute')"
        ),
        {
            "workspace": workspace,
            "repo": remote_repo,
            "head": "a" * 40,
            "run": live_run,
            "url": live_url,
        },
    )
    db.commit()
    schema = str(db.scalar(text("SELECT current_schema()")))
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            first = await ledger.pending()
            assert len(first) == 100
            assert {row[0] for row in first} == set(stale_runs)
            target = BoundBitbucketTarget(installation, workspace, repository, remote_repo, 1, 1)
            snapshot = BitbucketStatusSnapshot(
                live_run, target, 7, "a" * 40, "feature/review", "succeeded", 2
            )

            class Source:
                async def current(self, run_id: UUID) -> BitbucketStatusSnapshot | None:
                    return snapshot if run_id == live_run else None

            class Credentials:
                async def checkpoint(self, selected: BoundBitbucketTarget) -> None:
                    assert selected == target

                async def credential_for(
                    self, selected: BoundBitbucketTarget
                ) -> BitbucketRequestCredential:
                    return BitbucketRequestCredential(selected, "test-token")

            posted: list[str] = []

            def respond(request: httpx.Request) -> httpx.Response:
                if request.method == "GET":
                    return httpx.Response(
                        200,
                        json={
                            "id": 7,
                            "state": "OPEN",
                            "source": {
                                "commit": {"hash": snapshot.head_sha},
                                "branch": {"name": snapshot.source_branch},
                                "repository": {"uuid": f"{{{remote_repo}}}"},
                            },
                            "destination": {"repository": {"uuid": f"{{{remote_repo}}}"}},
                        },
                    )
                posted.append(request.method)
                return httpx.Response(201, json={})

            publisher = BitbucketBuildStatusPublisher(
                Source(),
                Credentials(),
                ledger=ledger,
                transport_factory=lambda: httpx.MockTransport(respond),
            )
            reconciler = BitbucketStatusReconciler(ledger, publisher)
            await reconciler.tick()
            assert await ledger.pending() == [(live_run, live_url)]
            await reconciler.tick()
            assert posted == ["POST"]
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT desired_state,applied_state,"
                            "(SELECT count(*) FROM bitbucket_status_publications) "
                            "FROM bitbucket_status_publications WHERE run_id=:run"
                        ),
                        {"run": live_run},
                    )
                ).one()
            assert row == ("succeeded", "succeeded", 1)
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET finished_at=now()-interval '2 hours' WHERE id=:run"),
                    {"run": live_run},
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET next_check_at=now() "
                        "WHERE run_id=:run"
                    ),
                    {"run": live_run},
                )
            assert await ledger.pending() == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
