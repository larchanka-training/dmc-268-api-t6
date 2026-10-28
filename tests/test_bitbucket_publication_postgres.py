"""Installed Bitbucket publication reloads immutable Run authority without a DB lease."""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

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
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.publish_run_review import (
    ObsoletePublication,
    PublicationIdentity,
    PublishContext,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.bitbucket_publication_resources import (
    BitbucketInstalledPublicationResources,
)
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)
pytestmark = pytest.mark.integration
HEAD = "a" * 40
BASE = "b" * 40


def test_installed_publication_checks_origin_and_releases_db_before_http(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo = (uuid4() for _ in range(4))
    change, run_id, rule, prompt = (uuid4() for _ in range(4))
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
                        "private-test-token",
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
                external_id = await session.scalar(
                    text("SELECT external_id FROM provider_installations WHERE id=:id"),
                    {"id": installation},
                )
                repository_external_id = await session.scalar(
                    text("SELECT external_id FROM repositories WHERE id=:id"),
                    {"id": repository},
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
                        "(:id,:repo,7,7,'PR','feature','main',:base,:head,'open',:url)"
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
                        "(:id,:change,:installation,:base,'main',:head,'publishing','webhook',"
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
            assert type(external_id) is int
            assert type(repository_external_id) is int
            finding = PublishedFinding(
                "src/app.py",
                3,
                None,
                "high",
                "correctness",
                "Wrong branch",
                "The guard is inverted.",
                None,
                0.9,
                None,
                finding_id=uuid4(),
            )
            locator = PullRequestLocator(
                external_id,
                "team/repo",
                7,
                provider="bitbucket",
                origin_provider_installation_id=installation,
                repository_external_id=repository_external_id,
                repository_uuid=remote_repo,
            )
            context = PublishContext(
                run_id,
                "publishing",
                False,
                HEAD,
                HEAD,
                True,
                True,
                external_id,
                "team/repo",
                7,
                "Review summary",
                (finding,),
                "f" * 64,
                "bitbucket",
                repository_external_id,
                workspace,
                RunVcsInput(change, repository, HEAD, BASE, locator),
                PublicationIdentity(
                    installation, workspace, external_id, "bitbucket", repository, change, BASE
                ),
            )
            comments: list[dict[str, object]] = []
            seen: list[httpx.Request] = []
            pause_next_write = False
            post_entered = asyncio.Event()
            allow_post = asyncio.Event()

            async def respond(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                assert request.url.host == "api.bitbucket.org"
                assert request.headers["Authorization"] == "Bearer private-test-token"
                async with engine.connect() as connection:
                    assert await connection.scalar(text("SELECT 1")) == 1
                if request.url.path.endswith("/pullrequests/7"):
                    return httpx.Response(
                        200,
                        json={
                            "id": 7,
                            "state": "OPEN",
                            "source": {
                                "commit": {"hash": HEAD},
                                "repository": {"uuid": f"{{{remote_repo}}}"},
                            },
                            "destination": {"repository": {"uuid": f"{{{remote_repo}}}"}},
                        },
                    )
                if request.url.path.endswith("/pullrequests/7/comments"):
                    if request.method == "GET":
                        return httpx.Response(200, json={"values": comments})
                    assert request.method == "POST"
                    if pause_next_write:
                        post_entered.set()
                        await allow_post.wait()
                    payload = json.loads(request.content)
                    result: dict[str, object] = {"id": len(comments) + 1, **payload}
                    comments.append(result)
                    return httpx.Response(201, json=result)
                if request.url.path.endswith("/pullrequests/7/comments/2"):
                    assert request.method == "PUT"
                    if pause_next_write:
                        post_entered.set()
                        await allow_post.wait()
                    payload = json.loads(request.content)
                    comments[1].update(payload)
                    return httpx.Response(200, json=comments[1])
                raise AssertionError(request.url.path)

            resources = BitbucketInstalledPublicationResources(
                session_factory=sessions,
                cipher=cipher,
                transport_factory=lambda: httpx.MockTransport(respond),
                heartbeat_interval_seconds=3600,
            )
            competing = BitbucketInstalledPublicationResources(
                session_factory=sessions,
                cipher=cipher,
                transport_factory=lambda: httpx.MockTransport(respond),
                heartbeat_interval_seconds=3600,
            )

            async def checkpoint() -> None:
                return None

            submission = ReviewSubmission(
                external_id, "team/repo", 7, HEAD, "COMMENT", "Review summary", (finding,), "f" * 64
            )
            owner_ready = asyncio.Event()
            contender_attempted = asyncio.Event()

            async def publish_first_pointer() -> SubmittedReview:
                async with resources.open(context, checkpoint=checkpoint) as gateway:
                    owner_ready.set()
                    await contender_attempted.wait()
                    assert comments == []
                    return await gateway.submit_review(submission)

            async def publish_second_pointer() -> None:
                await owner_ready.wait()
                try:
                    with pytest.raises(ObsoletePublication):
                        async with competing.open(context, checkpoint=checkpoint):
                            pytest.fail("concurrent pointer acquired the same publication")
                finally:
                    contender_attempted.set()

            first, _ = await asyncio.wait_for(
                asyncio.gather(publish_first_pointer(), publish_second_pointer()), timeout=10
            )
            async with competing.open(context, checkpoint=checkpoint) as gateway:
                repeated = await gateway.submit_review(submission)
            assert first == repeated
            assert len(comments) == 2
            assert len([request for request in seen if request.method == "POST"]) == 2

            # A worker whose lease expired cannot write after a second process claims it.
            async with resources.open(context, checkpoint=checkpoint) as stale_gateway:
                async with sessions.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE bitbucket_publication_claims "
                            "SET lease_until=now()-interval '1 second' WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    )
                async with competing.open(context, checkpoint=checkpoint) as current_gateway:
                    changed = ReviewSubmission(
                        external_id,
                        "team/repo",
                        7,
                        HEAD,
                        "COMMENT",
                        "Revised summary",
                        (),
                        "e" * 64,
                    )
                    with pytest.raises(ObsoletePublication):
                        await stale_gateway.submit_review(changed)
                    await current_gateway.submit_review(changed)
            assert len([request for request in seen if request.method == "POST"]) == 2
            assert len([request for request in seen if request.method == "PUT"]) == 1

            # Renewal continues while provider HTTP is waiting; an expired publishing
            # pointer cannot take over a slow request from a live worker.
            fast_heartbeat = BitbucketInstalledPublicationResources(
                session_factory=sessions,
                cipher=cipher,
                transport_factory=lambda: httpx.MockTransport(respond),
                heartbeat_interval_seconds=0.01,
            )
            pause_next_write = True
            slow_submission = ReviewSubmission(
                external_id, "team/repo", 7, HEAD, "COMMENT", "Slow summary", (), "d" * 64
            )
            async with fast_heartbeat.open(context, checkpoint=checkpoint) as slow_gateway:
                slow_task = asyncio.create_task(slow_gateway.submit_review(slow_submission))
                try:
                    await asyncio.wait_for(post_entered.wait(), timeout=2)
                    async with sessions.begin() as session:
                        await session.execute(
                            text(
                                "UPDATE bitbucket_publication_claims "
                                "SET lease_until=now()+interval '30 milliseconds' "
                                "WHERE run_id=:run"
                            ),
                            {"run": run_id},
                        )
                    await asyncio.sleep(0.1)
                    with pytest.raises(ObsoletePublication):
                        async with competing.open(context, checkpoint=checkpoint):
                            pytest.fail("live heartbeat lost the publication claim")
                finally:
                    allow_post.set()
                    await asyncio.wait_for(slow_task, timeout=2)
            assert len([request for request in seen if request.method == "POST"]) == 2
            assert len([request for request in seen if request.method == "PUT"]) == 2

            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_connections SET credential_generation=2 "
                        "WHERE provider_installation_id=:id"
                    ),
                    {"id": installation},
                )
            with pytest.raises(RunCancelled, match="installation_unavailable"):
                async with resources.open(context, checkpoint=checkpoint):
                    pass
            assert len([request for request in seen if request.method == "POST"]) == 2
            assert len([request for request in seen if request.method == "PUT"]) == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())
