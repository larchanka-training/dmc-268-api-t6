"""Signed push refresh and connection management share one PostgreSQL lock order."""

from __future__ import annotations

import asyncio
import os
from functools import partial
from uuid import uuid4

import pytest
from sqlalchemy import Connection, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.modules.code_index.application.request_bitbucket_index_refresh import (
    RequestBitbucketIndexRefresh,
)
from app.modules.code_index.infrastructure.bitbucket_index_refresh import (
    SqlAlchemyBitbucketIndexRefreshUnitOfWork,
)
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
from app.modules.code_index.infrastructure.models import CodeIndexState
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.models import BitbucketConnection
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.models import Workspace
from tests.test_app_reinstall_postgres import wait_for_blocked_backend
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)


@pytest.mark.integration
@pytest.mark.parametrize("management_action", ["rotate", "rebind"])
def test_refresh_waits_for_connection_management_before_repository_lock(
    bitbucket_connection_schema: Connection, management_action: str
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace_id, installation_id, repository_uuid = uuid4(), uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'index',7)"),
        {"id": workspace_id},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> None:
        try:
            async with factory.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    bitbucket_workspace_uuid=uuid4(),
                    token=cipher.encrypt(
                        "initial-token",
                        context=CredentialContext(installation_id, workspace_id),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                repository_id, _ = await store.bind_repository(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    repository_uuid=repository_uuid,
                    full_name="team/repo",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/repo",
                )
                await session.execute(
                    update(Repository)
                    .where(Repository.id == repository_id)
                    .values(rag_enabled=True, enabled=False)
                )
                generation = await store.set_repository_webhook_secret(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    repository_id=repository_id,
                    secret=cipher.encrypt(
                        "hook-secret-012345678901234567890123",
                        context=CredentialContext(installation_id, workspace_id, repository_id),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
                await session.execute(
                    text(
                        "UPDATE bitbucket_repository_webhook_secrets "
                        "SET confirmed_at=clock_timestamp() WHERE repository_id=:id"
                    ),
                    {"id": repository_id},
                )
                await SqlAlchemyCodeIndexStore(session).enable(repository_id)
            refresh = RequestBitbucketIndexRefresh(
                partial(SqlAlchemyBitbucketIndexRefreshUnitOfWork, factory)
            )
            task: asyncio.Task[bool] | None = None
            try:
                async with factory() as manager, factory() as observer:
                    # Rotation/rebind already owns Workspace, Installation and
                    # Connection when the push reaches index refresh.
                    assert (
                        await manager.scalar(
                            select(Workspace.id)
                            .where(Workspace.id == workspace_id)
                            .with_for_update()
                        )
                        == workspace_id
                    )
                    assert (
                        await manager.scalar(
                            select(ProviderInstallation.id)
                            .where(ProviderInstallation.id == installation_id)
                            .with_for_update()
                        )
                        == installation_id
                    )
                    assert (
                        await manager.scalar(
                            select(BitbucketConnection.provider_installation_id)
                            .where(BitbucketConnection.provider_installation_id == installation_id)
                            .with_for_update()
                        )
                        == installation_id
                    )
                    blocker_pid = await manager.scalar(text("SELECT pg_backend_pid()"))
                    assert isinstance(blocker_pid, int)
                    task = asyncio.create_task(
                        refresh.execute(
                            installation_id,
                            repository_id,
                            repository_uuid,
                            frozenset({"main"}),
                            1,
                            generation,
                        )
                    )
                    await wait_for_blocked_backend(observer, blocker_pid, task)
                    store = SqlAlchemyBitbucketConnectionStore(manager)
                    if management_action == "rotate":
                        assert (
                            await store.rotate_token(
                                workspace_id=workspace_id,
                                installation_id=installation_id,
                                token=cipher.encrypt(
                                    "rotated-token",
                                    context=CredentialContext(installation_id, workspace_id),
                                    purpose=CredentialPurpose.TOKEN,
                                ),
                            )
                            == 2
                        )
                    else:
                        rebound_id, reactivated = await asyncio.wait_for(
                            store.bind_repository(
                                workspace_id=workspace_id,
                                installation_id=installation_id,
                                repository_uuid=repository_uuid,
                                full_name="team/renamed",
                                default_branch="main",
                                web_url="https://bitbucket.org/team/renamed",
                            ),
                            5,
                        )
                        assert rebound_id == repository_id and not reactivated
                    await manager.commit()
                assert await asyncio.wait_for(task, 5)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            async with factory() as verify:
                state = await verify.get(CodeIndexState, repository_id)
                assert state is not None and state.wake_requested_at is not None

            # The confirmed webhook may still deliver after the user disables RAG.
            # Review is already disabled, so this exercises the independent RAG gate.
            async with factory.begin() as session:
                await session.execute(
                    update(Repository)
                    .where(Repository.id == repository_id)
                    .values(rag_enabled=False)
                )
                await session.execute(
                    update(CodeIndexState)
                    .where(CodeIndexState.repository_id == repository_id)
                    .values(wake_requested_at=None)
                )
            assert not await refresh.execute(
                installation_id,
                repository_id,
                repository_uuid,
                frozenset({"main"}),
                1,
                generation,
            )
            async with factory() as verify:
                state = await verify.get(CodeIndexState, repository_id)
                assert state is not None and state.wake_requested_at is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
