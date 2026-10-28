"""Bitbucket worker attempt uses the stored run origin and a detached credential read."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket_reviews import BitbucketReviewWorkerResources
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import (
    get_cancel_run,
    get_rerun_uow_factory,
    get_run_publisher,
    get_run_repository,
)
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.reviews.application.cancel_run import CancelRun
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.rerun_run import RerunOriginConflict, RerunRun
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunUnitOfWork
from app.modules.reviews.infrastructure.run_repository import (
    SqlAlchemyCancelRunUnitOfWork,
    SqlAlchemyRunRepository,
)
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)
pytestmark = pytest.mark.integration
HEAD = "a" * 40
BASE = "b" * 40


def test_persisted_origin_admits_bitbucket_http_without_holding_db_lease(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo = (uuid4() for _ in range(4))
    change, run_id, rule, prompt = (uuid4() for _ in range(4))
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    db.commit()
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
                    full_name="team/old-name",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/old-name",
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
                        "(id,repository_id,external_id,external_number,title,"
                        "source_branch,target_branch,base_sha,head_sha,state,web_url) VALUES "
                        "(:id,:repo,7,7,'PR','feature','main',:base,:head,'open',:url)"
                    ),
                    {
                        "id": change,
                        "repo": repository,
                        "base": BASE,
                        "head": HEAD,
                        "url": "https://bitbucket.org/team/old-name/pull-requests/7",
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
                        "base_sha,base_ref,head_sha,state,trigger,idempotency_key,engine,"
                        "rule_version_id,prompt_version_id,available_at) VALUES "
                        "(:id,:change,:installation,:base,'main',:head,'queued','webhook',"
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
            expected = await SqlAlchemyRunRepository(
                sessions, allow_unscoped=True
            ).get_run_vcs_input(run_id)
            assert expected is not None
            assert expected.base_ref == "main"
            claimed = ClaimedAttempt(
                run_id,
                workspace,
                1,
                "fast",
                datetime.now(UTC) + timedelta(minutes=1),
                prompt,
                rule,
                "test-worker",
            )
            seen: list[httpx.Request] = []

            async def respond(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                assert request.url.host == "api.bitbucket.org"
                assert request.headers["Authorization"] == "Bearer private-test-token"
                async with engine.connect() as connection:
                    assert await connection.scalar(text("SELECT 1")) == 1
                if request.url.path.endswith("/pullrequests/7"):
                    from tests.test_bitbucket_vcs import _pr

                    payload = _pr(base="d" * 12, source=remote_repo)
                    destination = payload["destination"]
                    assert isinstance(destination, dict)
                    destination["repository"] = {"uuid": "{" + str(remote_repo) + "}"}
                    return httpx.Response(200, json=payload)
                if request.url.path.endswith(f"/diffstat/{HEAD}..{BASE}"):
                    return httpx.Response(
                        200, json={"size": 0, "page": 1, "pagelen": 100, "values": []}
                    )
                raise AssertionError(request.url.path)

            resources = BitbucketReviewWorkerResources(
                session_factory=sessions,
                cipher=cipher,
                transport_factory=lambda: httpx.MockTransport(respond),
            )

            async def checkpoint() -> None:
                assert (
                    await SqlAlchemyRunRepository(sessions, allow_unscoped=True).get_run_vcs_input(
                        run_id
                    )
                    == expected
                )

            async with resources.attempt(
                claimed=claimed, expected_input=expected, checkpoint=checkpoint
            ) as bundle:
                assert (
                    await bundle.vcs.get_pull_request(expected.locator, checkpoint=checkpoint)
                ).head_sha == HEAD
                assert seen
                async with sessions.begin() as session:
                    await session.execute(
                        text("UPDATE runs SET state='succeeded' WHERE id=:id"),
                        {"id": run_id},
                    )
                app.dependency_overrides[get_auth_scope] = lambda: AuthScope(1, (workspace,))
                app.dependency_overrides[get_run_repository] = lambda: SqlAlchemyRunRepository(
                    sessions, allow_unscoped=True
                )
                app.dependency_overrides[get_rerun_uow_factory] = lambda: (
                    lambda: SqlAlchemyRerunUnitOfWork(sessions, allow_unscoped=True)
                )
                app.dependency_overrides[get_run_publisher] = lambda: None
                app.dependency_overrides[get_cancel_run] = lambda: CancelRun(
                    uow_factory=lambda: SqlAlchemyCancelRunUnitOfWork(sessions, allow_unscoped=True)
                )
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test"
                    ) as browser:
                        response = await browser.post(f"/api/runs/{run_id}/rerun")
                        assert response.status_code == 202, response.text
                        rerun_id = UUID(response.json()["id"])
                        assert rerun_id != run_id
                        detail_response = await browser.get(f"/api/runs/{rerun_id}")
                        assert detail_response.status_code == 200
                        listing = await browser.get("/api/runs")
                        assert listing.status_code == 200
                        assert {item["id"] for item in listing.json()["items"]} == {
                            str(run_id),
                            str(rerun_id),
                        }
                        cancellation = await browser.post(f"/api/runs/{rerun_id}/cancel")
                        assert cancellation.status_code == 200
                        assert cancellation.json()["id"] == str(rerun_id)
                        async with sessions.begin() as session:
                            await session.execute(
                                text("UPDATE runs SET state='succeeded' WHERE id=:id"),
                                {"id": rerun_id},
                            )
                            await SqlAlchemyBitbucketConnectionStore(session).rotate_token(
                                workspace_id=workspace,
                                installation_id=installation,
                                token=cipher.encrypt(
                                    "private-test-token",
                                    context=CredentialContext(installation, workspace),
                                    purpose=CredentialPurpose.TOKEN,
                                ),
                            )
                        rotated = await browser.post(f"/api/runs/{run_id}/rerun")
                        assert rotated.status_code == 202, rotated.text
                        rotated_id = UUID(rotated.json()["id"])
                        async with sessions.begin() as session:
                            await session.execute(
                                text("UPDATE runs SET state='succeeded' WHERE id=:id"),
                                {"id": rotated_id},
                            )
                            await SqlAlchemyBitbucketConnectionStore(
                                session
                            ).set_repository_webhook_secret(
                                workspace_id=workspace,
                                installation_id=installation,
                                repository_id=repository,
                                secret=cipher.encrypt(
                                    "n" * 40,
                                    context=CredentialContext(installation, workspace, repository),
                                    purpose=CredentialPurpose.WEBHOOK_SECRET,
                                ),
                            )
                        secret_rotated = await browser.post(f"/api/runs/{run_id}/rerun")
                        assert secret_rotated.status_code == 202, secret_rotated.text
                        secret_rotated_id = UUID(secret_rotated.json()["id"])
                finally:
                    app.dependency_overrides.clear()
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT repository_uuid FROM bitbucket_run_origins WHERE run_id=:id"
                            ),
                            {"id": rerun_id},
                        )
                        == remote_repo
                    )
                    assert (
                        await session.scalar(
                            text(
                                "SELECT credential_generation FROM bitbucket_run_origins "
                                "WHERE run_id=:id"
                            ),
                            {"id": rotated_id},
                        )
                        == 2
                    )
                    assert (
                        await session.scalar(
                            text(
                                "SELECT binding_generation FROM bitbucket_run_origins "
                                "WHERE run_id=:id"
                            ),
                            {"id": secret_rotated_id},
                        )
                        == 1
                    )
                assert (
                    await SqlAlchemyRunRepository(sessions, allow_unscoped=True).get_run_vcs_input(
                        secret_rotated_id
                    )
                    is not None
                )
                async with sessions.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE bitbucket_repository_webhook_secrets "
                            "SET binding_generation=binding_generation+1 WHERE repository_id=:id"
                        ),
                        {"id": repository},
                    )
                assert (
                    await SqlAlchemyRunRepository(sessions, allow_unscoped=True).get_run_vcs_input(
                        run_id
                    )
                    is None
                )
                assert (
                    await SqlAlchemyRunRepository(sessions, allow_unscoped=True).get_run_vcs_input(
                        rerun_id
                    )
                    is None
                )
                async with sessions.begin() as session:
                    await session.execute(
                        text("UPDATE runs SET state='succeeded' WHERE id=:id"),
                        {"id": secret_rotated_id},
                    )
                with pytest.raises(RerunOriginConflict):
                    await RerunRun(
                        lambda: SqlAlchemyRerunUnitOfWork(sessions, allow_unscoped=True),
                        SqlAlchemyRunRepository(sessions, allow_unscoped=True),
                        None,
                    ).execute(run_id)
                with pytest.raises(RunCancelled):
                    await bundle.vcs.source_repository_uuid(checkpoint=lambda: asyncio.sleep(0))
            with pytest.raises(RunCancelled):
                async with resources.attempt(
                    claimed=claimed, expected_input=expected, checkpoint=lambda: asyncio.sleep(0)
                ):
                    pass
        finally:
            await engine.dispose()

    asyncio.run(scenario())
