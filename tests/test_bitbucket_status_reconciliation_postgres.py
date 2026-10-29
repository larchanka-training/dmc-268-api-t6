"""Persisted status replay survives a missed terminal callback and old heads."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

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
    BitbucketStatusUnavailable,
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
    workspace, installation, repository, change, rule, prompt, live_run, expired_run = (
        uuid4() for _ in range(8)
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
        (expired_run, "e" * 64, "succeeded"),
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
    db.execute(
        text("UPDATE runs SET finished_at=now()-interval '2 hours' WHERE id=:run"),
        {"run": expired_run},
    )
    for i in range(100):
        db.execute(
            text(
                "INSERT INTO bitbucket_status_publications "
                "(workspace_id,repository_uuid,pr_number,head_sha,run_id,run_order,"
                "run_url,desired_state,desired_rank,applied_run_id,applied_state,next_check_at) "
                "VALUES (:workspace,:repo,:pr,:head,:run,1,:url,'succeeded',3,"
                ":run,'succeeded',now()-interval '1 minute')"
            ),
            {
                "workspace": workspace,
                "repo": remote_repo,
                "pr": i + 101,
                "head": f"{i + 100:040x}",
                "run": expired_run,
                "url": "https://review.example/expired",
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
                            "destination": {
                                "branch": {"name": "main"},
                                "repository": {"uuid": f"{{{remote_repo}}}"},
                            },
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
            async with sessions() as session:
                assert (
                    await session.scalar(text("SELECT count(*) FROM bitbucket_status_publications"))
                    == 1
                )
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


def test_running_run_requeued_reconciles_without_spin_or_duplicate_status(
    bitbucket_connection_schema: Connection,
) -> None:
    engine, workspace, installation, repository, run_id, remote_repo = _seed_status_run(
        bitbucket_connection_schema
    )

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            target = BoundBitbucketTarget(installation, workspace, repository, remote_repo, 1, 1)
            current_state = "running"

            class Source:
                async def current(self, selected: UUID) -> BitbucketStatusSnapshot | None:
                    assert selected == run_id
                    return BitbucketStatusSnapshot(
                        run_id, target, 7, "a" * 40, "feature/review", current_state, 1
                    )

            class Credentials:
                async def checkpoint(self, selected: BoundBitbucketTarget) -> None:
                    assert selected == target

                async def credential_for(
                    self, selected: BoundBitbucketTarget
                ) -> BitbucketRequestCredential:
                    return BitbucketRequestCredential(selected, "test-token")

            posts: list[httpx.Request] = []

            def respond(request: httpx.Request) -> httpx.Response:
                if request.method == "POST":
                    posts.append(request)
                    return httpx.Response(201, json={})
                return httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "state": "OPEN",
                        "source": {
                            "commit": {"hash": "a" * 40},
                            "branch": {"name": "feature/review"},
                            "repository": {"uuid": f"{{{remote_repo}}}"},
                        },
                        "destination": {
                            "branch": {"name": "main"},
                            "repository": {"uuid": f"{{{remote_repo}}}"},
                        },
                    },
                )

            publisher = BitbucketBuildStatusPublisher(
                Source(),
                Credentials(),
                ledger=ledger,
                transport_factory=lambda: httpx.MockTransport(respond),
            )
            run_url = "https://review.example/runs/one"
            await publisher.upsert(run_id, run_url)
            assert len(posts) == 1
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='queued' WHERE id=:run"), {"run": run_id}
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET next_check_at=now() "
                        "WHERE run_id=:run"
                    ),
                    {"run": run_id},
                )
            current_state = "queued"
            await publisher.upsert(run_id, run_url)
            assert len(posts) == 1
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT desired_state,applied_state "
                            "FROM bitbucket_status_publications WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    )
                ).one()
            assert row == ("queued", "queued")
            assert await ledger.pending() == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _seed_status_run(db: Connection) -> tuple[AsyncEngine, UUID, UUID, UUID, UUID, UUID]:
    command.upgrade(_config(db), "head")
    workspace, installation, repository, change, rule, prompt, run_id, remote_repo = (
        uuid4() for _ in range(8)
    )
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
            "(:id,:change,:installation,:base,'main',:head,'running','manual',"
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

    return engine, workspace, installation, repository, run_id, remote_repo


def test_terminal_retirement_keeps_unapplied_blocked_leased_and_nonterminal_rows(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    engine, workspace, _, _, run_id, remote_repo = _seed_status_run(db)
    db.execute(
        text(
            "UPDATE runs SET state='succeeded',finished_at=now()-interval '2 hours' WHERE id=:run"
        ),
        {"run": run_id},
    )
    rows = (
        (1, "succeeded", 3, run_id, "succeeded", None, None, None),
        (2, "succeeded", 3, run_id, "succeeded", None, "status_unavailable", None),
        (3, "publishing", 2, run_id, "publishing", None, None, None),
        (4, "succeeded", 3, None, None, None, None, None),
        (5, "succeeded", 3, run_id, "running", None, None, None),
        (6, "succeeded", 3, run_id, "succeeded", 1, None, None),
        (7, "succeeded", 3, run_id, "succeeded", None, None, uuid4()),
    )
    for pr, desired, rank, applied_run, applied, blocked, error, owner in rows:
        db.execute(
            text(
                "INSERT INTO bitbucket_status_publications "
                "(workspace_id,repository_uuid,pr_number,head_sha,run_id,run_order,run_url,"
                "desired_state,desired_rank,applied_run_id,applied_state,blocked_generation,"
                "last_error,owner_id,lease_until,next_check_at) VALUES "
                "(:workspace,:repo,:pr,:head,:run,1,'https://review.example/run',:desired,"
                ":rank,:applied_run,:applied,:blocked,:error,:owner,"
                "CASE WHEN CAST(:owner AS uuid) IS NULL THEN NULL "
                "ELSE now()+interval '1 minute' END,now())"
            ),
            {
                "workspace": workspace,
                "repo": remote_repo,
                "pr": pr,
                "head": f"{pr:040x}",
                "run": run_id,
                "desired": desired,
                "rank": rank,
                "applied_run": applied_run,
                "applied": applied,
                "blocked": blocked,
                "error": error,
                "owner": owner,
            },
        )
    db.commit()

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            assert await ledger.retire_expired() == 2
            async with sessions() as session:
                remaining = (
                    await session.scalars(
                        text(
                            "SELECT pr_number FROM bitbucket_status_publications ORDER BY pr_number"
                        )
                    )
                ).all()
            assert remaining == [3, 4, 5, 6, 7]
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET blocked_generation=NULL "
                        "WHERE pr_number=6"
                    )
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET owner_id=NULL,lease_until=NULL "
                        "WHERE pr_number=7"
                    )
                )
                await session.execute(
                    text("UPDATE runs SET state='running' WHERE id=:run"), {"run": run_id}
                )
            assert await ledger.retire_expired() == 0
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": run_id}
                )
            assert await ledger.retire_expired(limit=1) == 1
            assert await ledger.retire_expired() == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_terminal_retirement_skips_a_row_locked_by_another_worker(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    engine, workspace, _, _, run_id, remote_repo = _seed_status_run(db)
    db.execute(
        text(
            "UPDATE runs SET state='succeeded',finished_at=now()-interval '2 hours' WHERE id=:run"
        ),
        {"run": run_id},
    )
    db.execute(
        text(
            "INSERT INTO bitbucket_status_publications "
            "(workspace_id,repository_uuid,pr_number,head_sha,run_id,run_order,run_url,"
            "desired_state,desired_rank,applied_run_id,applied_state,next_check_at) VALUES "
            "(:workspace,:repo,7,:head,:run,1,'https://review.example/run','succeeded',3,"
            ":run,'succeeded',now())"
        ),
        {"workspace": workspace, "repo": remote_repo, "head": "a" * 40, "run": run_id},
    )
    db.commit()

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            async with sessions.begin() as holder:
                await holder.execute(
                    text(
                        "SELECT 1 FROM bitbucket_status_publications WHERE run_id=:run FOR UPDATE"
                    ),
                    {"run": run_id},
                )
                assert await asyncio.wait_for(ledger.retire_expired(), timeout=1.0) == 0
                await holder.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET owner_id=:owner,"
                        "lease_until=now()+interval '1 minute' WHERE run_id=:run"
                    ),
                    {"owner": uuid4(), "run": run_id},
                )
            assert await ledger.retire_expired() == 0
            async with sessions() as session:
                assert (
                    await session.scalar(text("SELECT count(*) FROM bitbucket_status_publications"))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("server_error", "status_unavailable"),
        ("missing_pr", "pr_missing"),
        ("missing_status", "status_unavailable"),
        ("declined_pr", "pr_not_open"),
        ("read_timeout", "status_unavailable"),
        ("connect_error", "status_unavailable"),
    ],
)
def test_status_failure_without_retry_after_is_deferred_and_not_selected_next_tick(
    bitbucket_connection_schema: Connection, failure: str, expected_reason: str
) -> None:
    engine, workspace, installation, repository, run_id, remote_repo = _seed_status_run(
        bitbucket_connection_schema
    )

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            target = BoundBitbucketTarget(installation, workspace, repository, remote_repo, 1, 1)
            snapshot = BitbucketStatusSnapshot(
                run_id, target, 7, "a" * 40, "feature/review", "running", 1
            )

            class Source:
                async def current(self, selected: UUID) -> BitbucketStatusSnapshot | None:
                    assert selected == run_id
                    return snapshot

            class Credentials:
                async def checkpoint(self, selected: BoundBitbucketTarget) -> None:
                    assert selected == target

                async def credential_for(
                    self, selected: BoundBitbucketTarget
                ) -> BitbucketRequestCredential:
                    return BitbucketRequestCredential(selected, "test-token")

            requests: list[httpx.Request] = []

            def respond(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                if failure == "read_timeout":
                    raise httpx.ReadTimeout("test timeout", request=request)
                if failure == "connect_error":
                    raise httpx.ConnectError("test connection error", request=request)
                if failure == "missing_status" and request.method == "POST":
                    return httpx.Response(404)
                if failure == "server_error":
                    return httpx.Response(503)
                if failure == "missing_pr":
                    return httpx.Response(404)
                return httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "state": "DECLINED" if failure == "declined_pr" else "OPEN",
                        "source": {
                            "commit": {"hash": snapshot.head_sha},
                            "branch": {"name": snapshot.source_branch},
                            "repository": {"uuid": f"{{{remote_repo}}}"},
                        },
                        "destination": {
                            "branch": {"name": "main"},
                            "repository": {"uuid": f"{{{remote_repo}}}"},
                        },
                    },
                )

            publisher = BitbucketBuildStatusPublisher(
                Source(),
                Credentials(),
                ledger=ledger,
                transport_factory=lambda: httpx.MockTransport(respond),
            )
            with pytest.raises(BitbucketStatusUnavailable):
                await publisher.upsert(run_id, "https://review.example/runs/one")
            expected_requests = 2 if failure == "missing_status" else 1
            assert len(requests) == expected_requests
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT last_error,next_check_at > now() "
                            "FROM bitbucket_status_publications WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    )
                ).one()
            assert row == (expected_reason, True)
            assert await ledger.pending() == []
            await publisher.upsert(run_id, "https://review.example/runs/one")
            await BitbucketStatusReconciler(ledger, publisher).tick()
            assert len(requests) == expected_requests
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET next_check_at=now() "
                        "WHERE run_id=:run"
                    ),
                    {"run": run_id},
                )
            assert await ledger.pending() == [(run_id, "https://review.example/runs/one")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cancelled_fork_with_missing_head_retires_unpublishable_status(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    engine, workspace, installation, repository, run_id, remote_repo = _seed_status_run(db)
    db.execute(text("UPDATE runs SET state='cancelled' WHERE id=:run"), {"run": run_id})
    db.execute(
        text(
            "UPDATE code_changes SET state='closed' "
            "WHERE id=(SELECT code_change_id FROM runs WHERE id=:run)"
        ),
        {"run": run_id},
    )
    db.commit()

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            target = BoundBitbucketTarget(installation, workspace, repository, remote_repo, 1, 1)
            snapshot = BitbucketStatusSnapshot(
                run_id,
                target,
                7,
                "a" * 40,
                "feature/review",
                "cancelled",
                1,
                target_branch="main",
            )

            class Source:
                async def current(self, selected: UUID) -> BitbucketStatusSnapshot | None:
                    assert selected == run_id
                    return snapshot

            class Credentials:
                async def checkpoint(self, selected: BoundBitbucketTarget) -> None:
                    assert selected == target

                async def credential_for(
                    self, selected: BoundBitbucketTarget
                ) -> BitbucketRequestCredential:
                    return BitbucketRequestCredential(selected, "test-token")

            requests: list[httpx.Request] = []

            def respond(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "state": "DECLINED",
                        "source": {
                            "commit": None,
                            "branch": {"name": "feature/review"},
                            "repository": {"uuid": f"{{{uuid4()}}}"},
                        },
                        "destination": {
                            "branch": {"name": "main"},
                            "repository": {"uuid": f"{{{remote_repo}}}"},
                        },
                    },
                )

            publisher = BitbucketBuildStatusPublisher(
                Source(),
                Credentials(),
                ledger=ledger,
                transport_factory=lambda: httpx.MockTransport(respond),
            )
            await publisher.upsert(run_id, "https://review.example/run")
            assert len(requests) == 1
            assert await ledger.pending() == []
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM bitbucket_status_publications WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retargeted_run_replaces_running_status_with_stopped_on_pinned_head(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    engine, workspace, installation, repository, run_id, remote_repo = _seed_status_run(db)

    async def scenario() -> None:
        try:
            sessions = async_sessionmaker(engine)
            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            target = BoundBitbucketTarget(installation, workspace, repository, remote_repo, 1, 1)
            snapshot = BitbucketStatusSnapshot(
                run_id,
                target,
                7,
                "a" * 40,
                "feature/review",
                "running",
                1,
                target_branch="main",
            )
            remote_target_branch = "main"

            class Source:
                async def current(self, selected: UUID) -> BitbucketStatusSnapshot | None:
                    assert selected == run_id
                    return snapshot

            class Credentials:
                async def checkpoint(self, selected: BoundBitbucketTarget) -> None:
                    assert selected == target

                async def credential_for(
                    self, selected: BoundBitbucketTarget
                ) -> BitbucketRequestCredential:
                    return BitbucketRequestCredential(selected, "test-token")

            posts: list[dict[str, str]] = []

            def respond(request: httpx.Request) -> httpx.Response:
                if request.method == "POST":
                    parsed: dict[str, str] = json.loads(request.content)
                    posts.append(parsed)
                    return httpx.Response(201, json=parsed)
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
                        "destination": {
                            "branch": {"name": remote_target_branch},
                            "repository": {"uuid": f"{{{remote_repo}}}"},
                        },
                    },
                )

            publisher = BitbucketBuildStatusPublisher(
                Source(),
                Credentials(),
                ledger=ledger,
                transport_factory=lambda: httpx.MockTransport(respond),
            )
            await publisher.upsert(run_id, "https://review.example/run")
            assert [post["state"] for post in posts] == ["INPROGRESS"]
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='cancelled' WHERE id=:run"), {"run": run_id}
                )
            snapshot = replace(snapshot, state="cancelled")
            remote_target_branch = "release"
            await publisher.upsert(run_id, "https://review.example/run")
            assert [post["state"] for post in posts] == ["INPROGRESS", "STOPPED"]
            assert posts[0]["key"] == posts[1]["key"]
            async with sessions() as session:
                applied = (
                    await session.execute(
                        text(
                            "SELECT desired_state,applied_state FROM bitbucket_status_publications "
                            "WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    )
                ).one()
            assert applied == ("cancelled", "cancelled")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
