"""Installed Bitbucket status reads one active immutable Run origin."""

from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.reviews.application.bitbucket_status import BitbucketStatusSnapshot
from app.modules.reviews.application.bitbucket_status_intents import (
    BitbucketStatusIntents,
    StatusKey,
)
from app.modules.reviews.application.run_coverage import CoverageItem, CoverageStatus, RunCoverage
from app.modules.reviews.application.verdict import severity_counts
from app.modules.reviews.infrastructure.bitbucket_ci import (
    BitbucketBuildStatusPublisher,
    BitbucketStatusForbidden,
    BitbucketStatusReconciler,
    BitbucketStatusUnavailable,
    SqlAlchemyBitbucketStatusSource,
)
from app.modules.reviews.infrastructure.bitbucket_status_ledger import (
    SqlAlchemyBitbucketStatusUnitOfWork,
)
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)
pytestmark = pytest.mark.integration
HEAD = "a" * 40
BASE = "b" * 40


def test_status_uses_active_run_origin_and_no_db_transaction_over_http(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace = UUID("11111111-1111-4111-8111-111111111111")
    remote_repo = UUID("22222222-2222-4222-8222-222222222222")
    installation, remote_workspace = (uuid4() for _ in range(2))
    change, rule, prompt = (uuid4() for _ in range(3))
    run_id = UUID(int=2**127 + 5)
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    db.commit()
    schema = str(db.scalar(text("SELECT current_schema()")))
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"k" * 32})

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace,
                    installation_id=installation,
                    bitbucket_workspace_uuid=remote_workspace,
                    token=cipher.encrypt(
                        "status-token",
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
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.system',1,'prompt',:checksum,true)"
                    ),
                    {"id": prompt, "checksum": "c" * 64},
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id,repository_id,version,rules,checksum,is_active) "
                        "VALUES (:id,:repo,1,'[]'::jsonb,:checksum,true)"
                    ),
                    {"id": rule, "repo": repository, "checksum": "d" * 64},
                )
                await session.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id,repository_id,external_id,external_number,title,source_branch,"
                        "target_branch,base_sha,head_sha,state,web_url) VALUES "
                        "(:id,:repo,7,7,'PR','feature/review','main',:base,:head,'open',:url)"
                    ),
                    {
                        "id": change,
                        "repo": repository,
                        "base": BASE,
                        "head": HEAD,
                        "url": "https://bitbucket.org/team/repo/pull-requests/7",
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
                        "base_sha,base_ref,head_sha,state,trigger,idempotency_key,engine,"
                        "rule_version_id,prompt_version_id,available_at) VALUES "
                        "(:id,:change,:installation,:base,'main',:head,'running','webhook',"
                        ":key,'fast',:rule,:prompt,now())"
                    ),
                    {
                        "id": run_id,
                        "change": change,
                        "installation": installation,
                        "base": BASE,
                        "head": HEAD,
                        "key": "1" * 64,
                        "rule": rule,
                        "prompt": prompt,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO bitbucket_run_origins "
                        "(run_id,workspace_id,provider_installation_id,repository_id,repository_uuid,"
                        "binding_generation,credential_generation,pr_number,head_sha) VALUES "
                        "(:run,:workspace,:installation,:repo,:remote,1,1,7,:head)"
                    ),
                    {
                        "run": run_id,
                        "workspace": workspace,
                        "installation": installation,
                        "repo": repository,
                        "remote": remote_repo,
                        "head": HEAD,
                    },
                )
            reader = SqlAlchemyBitbucketTargetReader(sessions)
            requests: list[httpx.Request] = []
            applied: list[str] = []
            hold_running = False
            ambiguous_terminal = False
            remote_pr_state = "OPEN"
            forbid_key: str | None = None
            rate_limit_status = False
            running_started = asyncio.Event()
            release_running = asyncio.Event()

            async def respond(request: httpx.Request) -> httpx.Response:
                nonlocal ambiguous_terminal
                requests.append(request)
                assert request.headers["Authorization"] == "Bearer status-token"
                async with engine.connect() as connection:
                    assert await connection.scalar(text("SELECT 1")) == 1
                if request.method == "GET":
                    if rate_limit_status:
                        return httpx.Response(429, headers={"Retry-After": "3600"})
                    return httpx.Response(
                        200,
                        json={
                            "id": 7,
                            "state": remote_pr_state,
                            "source": {
                                "commit": {"hash": HEAD},
                                "branch": {"name": "feature/review"},
                                "repository": {"uuid": f"{{{remote_repo}}}"},
                            },
                            "destination": {
                                "branch": {"name": "main"},
                                "repository": {"uuid": f"{{{remote_repo}}}"},
                            },
                        },
                    )
                payload: dict[str, str] = json.loads(request.content)
                if payload["key"] == forbid_key:
                    return httpx.Response(403, json={"error": "status author differs"})
                if hold_running and payload["state"] == "INPROGRESS":
                    running_started.set()
                    await release_running.wait()
                applied.append(payload["state"])
                if ambiguous_terminal and payload["state"] == "SUCCESSFUL":
                    ambiguous_terminal = False
                    raise httpx.ReadTimeout("status accepted before response")
                return httpx.Response(201, json=payload)

            ledger = BitbucketStatusIntents(lambda: SqlAlchemyBitbucketStatusUnitOfWork(sessions))
            publisher = BitbucketBuildStatusPublisher(
                SqlAlchemyBitbucketStatusSource(sessions),
                BitbucketRequestCredentials(reader, cipher),
                transport_factory=lambda: httpx.MockTransport(respond),
                ledger=ledger,
            )
            await publisher.upsert(run_id, "https://review.example/runs/one")
            assert len([request for request in requests if request.method == "POST"]) == 1

            # A later status event and the reconciler must honor a stored 429 delay.
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications "
                        "SET next_check_at=now()-interval '1 second', "
                        "applied_run_id=NULL, applied_state=NULL "
                        "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                        "AND pr_number=7 AND head_sha=:head"
                    ),
                    {"workspace": workspace, "repo": remote_repo, "head": HEAD},
                )
            rate_limit_status = True
            with pytest.raises(BitbucketStatusUnavailable):
                await publisher.upsert(run_id, "https://review.example/runs/one")
            after_limit = len(requests)
            await publisher.upsert(run_id, "https://review.example/runs/one")
            await BitbucketStatusReconciler(ledger, publisher).tick()
            assert len(requests) == after_limit
            async with sessions() as session:
                deferred = (
                    await session.execute(
                        text(
                            "SELECT next_check_at > now(), last_error "
                            "FROM bitbucket_status_publications "
                            "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                            "AND pr_number=7 AND head_sha=:head"
                        ),
                        {"workspace": workspace, "repo": remote_repo, "head": HEAD},
                    )
                ).one()
            assert deferred == (True, "rate_limited")
            rate_limit_status = False
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications "
                        "SET next_check_at=now(), last_error=NULL "
                        "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                        "AND pr_number=7 AND head_sha=:head"
                    ),
                    {"workspace": workspace, "repo": remote_repo, "head": HEAD},
                )

            # No subsequent Run event is needed to repair an ambiguous terminal write.
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": run_id}
                )
                coverage = RunCoverage(
                    CoverageStatus.NONE,
                    0,
                    reasons=(CoverageItem("src/app.py", "too_large"),),
                    head_sha=HEAD,
                    diff_digest="a" * 64,
                    snapshot_digest="b" * 64,
                    total_files=1,
                    file_list_complete=True,
                )
                await session.execute(
                    text(
                        "INSERT INTO run_actions "
                        "(id,run_id,index,tool,request,response,started_at,duration_ms) "
                        "VALUES (:id,:run,1,'review.postprocess','{}'::jsonb,"
                        "CAST(:response AS jsonb),now(),0)"
                    ),
                    {
                        "id": uuid4(),
                        "run": run_id,
                        "response": json.dumps(
                            {
                                "verdict": None,
                                "coverage": coverage.to_json(),
                                "severity_counts": severity_counts(()),
                                "summary": {
                                    "problem": "Text changes were not reviewed.",
                                    "done_well": "No confirmed findings were reported.",
                                    "effort": "none",
                                },
                            }
                        ),
                    },
                )
            ambiguous_terminal = True
            with pytest.raises(BitbucketStatusUnavailable, match="transport unavailable"):
                await publisher.upsert(run_id, "https://review.example/runs/one")
            assert applied[-1] == "SUCCESSFUL"
            terminal_post = [request for request in requests if request.method == "POST"][-1]
            assert json.loads(terminal_post.content)["description"] == (
                "AI review: nothing reviewable"
            )
            await BitbucketStatusReconciler(ledger, publisher).tick()
            assert applied[-1] == "SUCCESSFUL"
            assert len([request for request in requests if request.method == "POST"]) == 2
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET next_check_at=now() "
                        "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                        "AND pr_number=7 AND head_sha=:head"
                    ),
                    {"workspace": workspace, "repo": remote_repo, "head": HEAD},
                )
            await BitbucketStatusReconciler(ledger, publisher).tick()
            assert len([request for request in requests if request.method == "POST"]) == 3
            await BitbucketStatusReconciler(ledger, publisher).tick()
            assert len([request for request in requests if request.method == "POST"]) == 3

            # A second portal workspace can bind the same remote PR/head. It
            # owns a separate ledger row, lease and provider status key.
            workspace2, installation2, change2, run2, rule2 = (uuid4() for _ in range(5))
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'other',7)"
                    ),
                    {"id": workspace2},
                )
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace2,
                    installation_id=installation2,
                    bitbucket_workspace_uuid=remote_workspace,
                    token=cipher.encrypt(
                        "status-token",
                        context=CredentialContext(installation2, workspace2),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                repository2, _ = await store.bind_repository(
                    workspace_id=workspace2,
                    installation_id=installation2,
                    repository_uuid=remote_repo,
                    full_name="team/repo",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/repo",
                )
                await store.set_repository_webhook_secret(
                    workspace_id=workspace2,
                    installation_id=installation2,
                    repository_id=repository2,
                    secret=cipher.encrypt(
                        "s" * 40,
                        context=CredentialContext(installation2, workspace2, repository2),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id,repository_id,version,rules,checksum,is_active) "
                        "VALUES (:id,:repo,1,'[]'::jsonb,:checksum,true)"
                    ),
                    {"id": rule2, "repo": repository2, "checksum": "e" * 64},
                )
                await session.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id,repository_id,external_id,external_number,title,source_branch,"
                        "target_branch,base_sha,head_sha,state,web_url) VALUES "
                        "(:id,:repo,7,7,'PR','feature/review','main',:base,:head,'open',:url)"
                    ),
                    {
                        "id": change2,
                        "repo": repository2,
                        "base": BASE,
                        "head": HEAD,
                        "url": "https://bitbucket.org/team/repo/pull-requests/7",
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
                        "base_sha,base_ref,head_sha,state,trigger,idempotency_key,engine,"
                        "rule_version_id,prompt_version_id,available_at) VALUES "
                        "(:id,:change,:installation,:base,'main',:head,'running','webhook',"
                        ":key,'fast',:rule,:prompt,now())"
                    ),
                    {
                        "id": run2,
                        "change": change2,
                        "installation": installation2,
                        "base": BASE,
                        "head": HEAD,
                        "key": "3" * 64,
                        "rule": rule2,
                        "prompt": prompt,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO bitbucket_run_origins "
                        "(run_id,workspace_id,provider_installation_id,repository_id,repository_uuid,"
                        "binding_generation,credential_generation,pr_number,head_sha) VALUES "
                        "(:run,:workspace,:installation,:repo,:remote,1,1,7,:head)"
                    ),
                    {
                        "run": run2,
                        "workspace": workspace2,
                        "installation": installation2,
                        "repo": repository2,
                        "remote": remote_repo,
                        "head": HEAD,
                    },
                )
            await publisher.upsert(run2, "https://review.example/runs/other")
            statuses = [
                json.loads(request.content) for request in requests if request.method == "POST"
            ]
            assert len(statuses) == 4
            assert statuses[-1]["key"] != statuses[-2]["key"]
            await publisher.upsert(run_id, "https://review.example/runs/one")
            assert len([request for request in requests if request.method == "POST"]) == 4

            # A manual rerun on the same PR/head owns this status key. Delayed
            # updates from the historical Run must not overwrite its transition.
            newer_run = UUID(int=run_id.int - 1)
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": run_id}
                )
                await session.execute(
                    text(
                        "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
                        "base_sha,base_ref,head_sha,state,trigger,idempotency_key,engine,"
                        "rule_version_id,prompt_version_id,available_at,created_at) VALUES "
                        "(:id,:change,:installation,:base,'main',:head,'running','manual',"
                        ":key,'fast',:rule,:prompt,now(),"
                        "(SELECT created_at FROM runs WHERE id=:old_run))"
                    ),
                    {
                        "id": newer_run,
                        "change": change,
                        "installation": installation,
                        "base": BASE,
                        "head": HEAD,
                        "key": "2" * 64,
                        "rule": rule,
                        "prompt": prompt,
                        "old_run": run_id,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO bitbucket_run_origins "
                        "(run_id,workspace_id,provider_installation_id,repository_id,repository_uuid,"
                        "binding_generation,credential_generation,pr_number,head_sha) VALUES "
                        "(:run,:workspace,:installation,:repo,:remote,1,1,7,:head)"
                    ),
                    {
                        "run": newer_run,
                        "workspace": workspace,
                        "installation": installation,
                        "repo": repository,
                        "remote": remote_repo,
                        "head": HEAD,
                    },
                )
            await publisher.upsert(run_id, "https://review.example/runs/one")
            assert len([request for request in requests if request.method == "POST"]) == 4
            await publisher.upsert(newer_run, "https://review.example/runs/two")
            assert applied[-1] == "INPROGRESS"
            newest_post = [request for request in requests if request.method == "POST"][-1]
            assert json.loads(newest_post.content) == {
                "key": "AI-REVIEW-356f9b3b40276a07ce72380b745f6c",
                "state": "INPROGRESS",
                "name": "AI Code Reviewer",
                "description": "AI review running",
                "url": "https://review.example/runs/two",
                "refname": "feature/review",
            }
            assert await SqlAlchemyBitbucketStatusSource(sessions).current(run_id) is None
            active_snapshot = await SqlAlchemyBitbucketStatusSource(sessions).current(newer_run)
            assert active_snapshot is not None
            assert active_snapshot.target_branch == "main"
            async with sessions() as session:
                old_order = await session.scalar(
                    text("SELECT run_order FROM bitbucket_run_origins WHERE run_id=:run"),
                    {"run": run_id},
                )
                newer_order = await session.scalar(
                    text("SELECT run_order FROM bitbucket_run_origins WHERE run_id=:run"),
                    {"run": newer_run},
                )
            assert isinstance(old_order, int) and isinstance(newer_order, int)
            assert old_order < newer_order
            stale = BitbucketStatusSnapshot(
                run_id,
                active_snapshot.target,
                7,
                HEAD,
                "feature/review",
                "succeeded",
                old_order,
            )
            await ledger.register(stale, "https://review.example/runs/one")
            await publisher.upsert(run_id, "https://review.example/runs/one")
            async with sessions() as session:
                ordered = (
                    await session.execute(
                        text(
                            "SELECT run_id,run_order,desired_state,applied_run_id,applied_state "
                            "FROM bitbucket_status_publications "
                            "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                            "AND pr_number=7 AND head_sha=:head"
                        ),
                        {"workspace": workspace, "repo": remote_repo, "head": HEAD},
                    )
                ).one()
            assert ordered == (newer_run, newer_order, "running", newer_run, "running")
            assert applied[-1] == "INPROGRESS"
            assert len([request for request in requests if request.method == "POST"]) == 5
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='publishing' WHERE id=:run"),
                    {"run": newer_run},
                )
            hold_running = True
            running_task = asyncio.create_task(
                publisher.upsert(newer_run, "https://review.example/runs/two")
            )
            await asyncio.wait_for(running_started.wait(), timeout=2)
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": newer_run}
                )
            terminal_task = asyncio.create_task(
                publisher.upsert(newer_run, "https://review.example/runs/two")
            )
            try:
                await asyncio.sleep(0.05)
            finally:
                release_running.set()
            await asyncio.wait_for(asyncio.gather(running_task, terminal_task), timeout=5)
            assert applied[-1] == "SUCCESSFUL"
            assert len([request for request in requests if request.method == "POST"]) == 7
            # PR close cancels the active Run and still writes a terminal status.
            remote_pr_state = "DECLINED"
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='cancelled' WHERE id=:run"), {"run": newer_run}
                )
                await session.execute(
                    text("UPDATE code_changes SET state='closed' WHERE id=:id"), {"id": change}
                )
            await publisher.upsert(newer_run, "https://review.example/runs/two")
            assert applied[-1] == "STOPPED"
            assert len([request for request in requests if request.method == "POST"]) == 8
            remote_pr_state = "OPEN"
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": newer_run}
                )
                await session.execute(
                    text("UPDATE code_changes SET state='open' WHERE id=:id"), {"id": change}
                )
            # A changed Bitbucket author cannot overwrite the existing key.
            # Persist the error and do not retry it on the leader tick.
            forbid_key = statuses[-1]["key"]
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": run2}
                )
            with pytest.raises(BitbucketStatusForbidden):
                await publisher.upsert(run2, "https://review.example/runs/other")
            async with sessions() as session:
                blocked = (
                    await session.execute(
                        text(
                            "SELECT blocked_generation,last_error "
                            "FROM bitbucket_status_publications "
                            "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                            "AND pr_number=7 AND head_sha=:head"
                        ),
                        {"workspace": workspace2, "repo": remote_repo, "head": HEAD},
                    )
                ).one()
            assert blocked == (1, "author_forbidden")
            post_count = len([request for request in requests if request.method == "POST"])
            await BitbucketStatusReconciler(ledger, publisher).tick()
            assert len([request for request in requests if request.method == "POST"]) == post_count
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_changes SET head_sha=:head WHERE id=:id"),
                    {"head": "c" * 40, "id": change},
                )
            await publisher.upsert(newer_run, "https://review.example/runs/two")
            assert len([request for request in requests if request.method == "POST"]) == 9
            key = StatusKey(workspace, remote_repo, 7, HEAD)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications SET next_check_at=now() "
                        "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                        "AND pr_number=7 AND head_sha=:head"
                    ),
                    {"workspace": workspace, "repo": remote_repo, "head": HEAD},
                )
            old_claim = await ledger.acquire(key)
            assert old_claim is not None
            old_intent = await ledger.intent(old_claim)
            assert old_intent is not None
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications "
                        "SET lease_until=now()-interval '1 second' "
                        "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                        "AND pr_number=7 AND head_sha=:head"
                    ),
                    {"workspace": workspace, "repo": remote_repo, "head": HEAD},
                )
            new_claim = await ledger.acquire(key)
            assert new_claim is not None
            assert new_claim.generation > old_claim.generation
            assert not await ledger.renew(old_claim)
            assert not await ledger.mark_applied(old_claim, old_intent)
            await ledger.release(new_claim)
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_changes SET head_sha=:head WHERE id=:id"),
                    {"head": HEAD, "id": change},
                )
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_connections SET credential_generation=2 "
                        "WHERE provider_installation_id=:id"
                    ),
                    {"id": installation},
                )
            await publisher.upsert(newer_run, "https://review.example/runs/two")
            posts_after_rotation = [request for request in requests if request.method == "POST"]
            assert len(posts_after_rotation) == 10
            assert json.loads(posts_after_rotation[-1].content)["state"] == "SUCCESSFUL"
            # A new credential generation may recover a prior status-author 403
            # without creating a new Run or changing the immutable head.
            forbid_key = None
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_connections SET credential_generation=2 "
                        "WHERE provider_installation_id=:id"
                    ),
                    {"id": installation2},
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_status_publications "
                        "SET applied_run_id=:run,applied_state='succeeded' "
                        "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                        "AND pr_number=7 AND head_sha=:head"
                    ),
                    {"run": run2, "workspace": workspace2, "repo": remote_repo, "head": HEAD},
                )
                await session.execute(
                    text("UPDATE runs SET finished_at=now()-interval '2 hours' WHERE id=:run"),
                    {"run": run2},
                )
            rotated = await SqlAlchemyBitbucketStatusSource(sessions).current(run2)
            assert rotated is not None
            assert rotated.target.credential_generation == 2
            assert (run2, "https://review.example/runs/other") in await ledger.pending()
            await publisher.replay(run2, "https://review.example/runs/other")
            assert len([request for request in requests if request.method == "POST"]) == 11
            async with sessions() as session:
                recovery = (
                    await session.execute(
                        text(
                            "SELECT blocked_generation,last_error,applied_state "
                            "FROM bitbucket_status_publications "
                            "WHERE workspace_id=:workspace AND repository_uuid=:repo "
                            "AND pr_number=7 AND head_sha=:head"
                        ),
                        {"workspace": workspace2, "repo": remote_repo, "head": HEAD},
                    )
                ).one()
            assert recovery == (None, None, "succeeded")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
