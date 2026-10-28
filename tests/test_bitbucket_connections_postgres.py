"""Bitbucket credential rows keep secret and tenant authority isolated (#333)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.bitbucket import get_bitbucket_management
from app.bootstrap.db_metadata import metadata
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application.manage_connection import (
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketTargetInactive,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
    SqlAlchemyBitbucketManagementUnitOfWork,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.credential_codec import (
    BitbucketCredentialCodecAdapter,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
    bitbucket_repository_external_id_seq,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
    BitbucketRepositorySnapshot,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketHttpClient,
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.infrastructure.models import Repository


@pytest.fixture
def bitbucket_connection_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL connection tests")
    schema = f"test_bitbucket_connection_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


@pytest.mark.integration
def test_bitbucket_connection_migration_matches_metadata(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    assert "bitbucket_connections" in {
        row[0]
        for row in db.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")
        )
    }
    assert (
        compare_metadata(
            MigrationContext.configure(
                db, opts={"compare_type": True, "compare_server_default": True}
            ),
            metadata,
        )
        == []
    )


@pytest.mark.integration
def test_two_workspace_credentials_rotate_and_revoke_independently(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    first_workspace, second_workspace = uuid4(), uuid4()
    for workspace in (first_workspace, second_workspace):
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
            {"id": str(workspace), "name": str(workspace)},
        )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    first_installation, second_installation = uuid4(), uuid4()
    remote_workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})

    async def scenario() -> None:
        database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg_async"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET search_path TO "{schema}"'))
                await connection.commit()
                async with AsyncSession(bind=connection) as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    for workspace, installation, token in (
                        (first_workspace, first_installation, "first-token"),
                        (second_workspace, second_installation, "second-token"),
                    ):
                        context = CredentialContext(installation, workspace)
                        await store.create_connection(
                            workspace_id=workspace,
                            installation_id=installation,
                            bitbucket_workspace_uuid=remote_workspace,
                            token=cipher.encrypt(
                                token, context=context, purpose=CredentialPurpose.TOKEN
                            ),
                        )
                        await store.bind_repository(
                            workspace_id=workspace,
                            installation_id=installation,
                            repository_uuid=remote_workspace,
                            full_name=f"{workspace}/private",
                            default_branch="main",
                            web_url=f"https://bitbucket.org/{workspace}/private",
                        )
                    await session.commit()
                    first_bindings = await store.list_bindings(workspace_id=first_workspace)
                    second_bindings = await store.list_bindings(workspace_id=second_workspace)
                    assert len(first_bindings) == len(second_bindings) == 1
                    assert first_bindings[0].installation_id == first_installation
                    assert second_bindings[0].installation_id == second_installation
                    assert first_bindings[0].repository_id != second_bindings[0].repository_id
                    rows = (
                        await session.execute(
                            text(
                                "SELECT provider_installation_id, workspace_id, "
                                "credential_generation, token_ciphertext "
                                "FROM bitbucket_connections "
                                "ORDER BY workspace_id"
                            )
                        )
                    ).all()
                    assert len(rows) == 2
                    assert all(row[2] == 1 for row in rows)
                    assert all(b"token" not in row[3] for row in rows)
                    with pytest.raises(IntegrityError) as error:
                        async with session.begin_nested():
                            await session.execute(
                                text(
                                    "UPDATE bitbucket_connections SET revoked_at=now() "
                                    "WHERE provider_installation_id=:id"
                                ),
                                {"id": first_installation},
                            )
                    assert getattr(error.value.orig, "sqlstate", None) == "23514"
                    with pytest.raises(ValueError, match="connection_inactive"):
                        await store.rotate_token(
                            workspace_id=first_workspace,
                            installation_id=second_installation,
                            token=cipher.encrypt(
                                "cross-workspace-token",
                                context=CredentialContext(second_installation, second_workspace),
                                purpose=CredentialPurpose.TOKEN,
                            ),
                        )
                    assert not await store.revoke_connection(
                        workspace_id=first_workspace, installation_id=second_installation
                    )
                    assert (
                        await store.rotate_token(
                            workspace_id=first_workspace,
                            installation_id=first_installation,
                            token=cipher.encrypt(
                                "rotated-token",
                                context=CredentialContext(first_installation, first_workspace),
                                purpose=CredentialPurpose.TOKEN,
                            ),
                        )
                        == 2
                    )
                    await session.commit()
                    assert await store.revoke_connection(
                        workspace_id=first_workspace, installation_id=first_installation
                    )
                    await session.commit()
                    first = (
                        await session.execute(
                            text(
                                "SELECT credential_generation, token_ciphertext, revoked_at "
                                "FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": str(first_installation)},
                        )
                    ).one()
                    second = (
                        await session.execute(
                            text(
                                "SELECT credential_generation, token_ciphertext, revoked_at "
                                "FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": str(second_installation)},
                        )
                    ).one()
                    assert first[0] == 3 and first[1] is None and first[2] is not None
                    assert second[0] == 1 and second[1] is not None and second[2] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_two_repositories_share_credential_and_disconnect_independently(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation = uuid4(), uuid4()
    remote_workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    first_remote, second_remote = uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    context = CredentialContext(installation, workspace)

    async def scenario() -> None:
        database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg_async"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET search_path TO "{schema}"'))
                await connection.commit()
                async with AsyncSession(bind=connection) as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    await store.create_connection(
                        workspace_id=workspace,
                        installation_id=installation,
                        bitbucket_workspace_uuid=remote_workspace,
                        token=cipher.encrypt(
                            "shared-token", context=context, purpose=CredentialPurpose.TOKEN
                        ),
                    )
                    first, first_created = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=first_remote,
                        full_name="nvdovnar/first",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/first",
                    )
                    second, second_created = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=second_remote,
                        full_name="nvdovnar/second",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/second",
                    )
                    for repository_id in (first, second):
                        await store.set_repository_webhook_secret(
                            workspace_id=workspace,
                            installation_id=installation,
                            repository_id=repository_id,
                            secret=cipher.encrypt(
                                "h" * 40,
                                context=CredentialContext(installation, workspace, repository_id),
                                purpose=CredentialPurpose.WEBHOOK_SECRET,
                            ),
                        )
                    await session.commit()
                    reader = SqlAlchemyBitbucketTargetReader(lambda: AsyncSession(bind=connection))
                    first_target = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=first_remote,
                    )
                    second_target = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=second_remote,
                    )
                    assert first_target.workspace_id == workspace
                    assert first_target.bitbucket_workspace_uuid == remote_workspace
                    assert first_target.workspace_id != first_target.bitbucket_workspace_uuid
                    assert first_created and second_created
                    assert first != second
                    assert (
                        await session.scalar(
                            text(
                                "SELECT credential_generation FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": installation},
                        )
                        == 1
                    )
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=first,
                    )
                    await session.commit()
                    rows = (
                        await session.execute(
                            select(Repository.id, Repository.installation_available)
                            .where(Repository.provider_installation_id == installation)
                            .order_by(Repository.id)
                        )
                    ).all()
                    assert {row[0]: row[1] for row in rows} == {first: False, second: True}
                    await reader.checkpoint(second_target)
                    with pytest.raises(BitbucketTargetInactive):
                        await reader.checkpoint(first_target)
                    rebound, newly_active = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=first_remote,
                        full_name="nvdovnar/first",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/first",
                    )
                    assert rebound == first and newly_active
                    await store.set_repository_webhook_secret(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=first,
                        secret=cipher.encrypt(
                            "n" * 40,
                            context=CredentialContext(installation, workspace, first),
                            purpose=CredentialPurpose.WEBHOOK_SECRET,
                        ),
                    )
                    await session.commit()
                    await reader.checkpoint(second_target)
                    with pytest.raises(BitbucketBindingGenerationChanged):
                        await reader.checkpoint(first_target)
                    assert (
                        await reader.resolve_target(
                            workspace_id=workspace,
                            installation_id=installation,
                            repository_uuid=first_remote,
                        )
                    ).repository_id == first
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=first,
                    )
                    await session.commit()
                    assert (
                        await session.scalar(
                            text(
                                "SELECT revoked_at FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": installation},
                        )
                        is None
                    )
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_id=second,
                    )
                    await session.commit()
                    assert (
                        await session.scalar(
                            text(
                                "SELECT revoked_at FROM bitbucket_connections "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": installation},
                        )
                        is not None
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_bitbucket_repository_enters_common_list_only_for_current_workspace_member(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo, other_workspace, other_installation = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    for portal_workspace in (workspace, other_workspace):
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
            {"id": portal_workspace},
        )
    for user_id in (42, 43):
        db.execute(
            text("INSERT INTO github_user_profiles (id, login, name) VALUES (:id, :login, :login)"),
            {"id": user_id, "login": f"user-{user_id}"},
        )
    for portal_workspace in (workspace, other_workspace):
        db.execute(
            text(
                "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
                "VALUES (42, :workspace)"
            ),
            {"workspace": portal_workspace},
        )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    context = CredentialContext(installation, workspace)
    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> tuple[UUID, UUID]:
        async with factory.begin() as session:
            store = SqlAlchemyBitbucketConnectionStore(session)
            await store.create_connection(
                workspace_id=workspace,
                installation_id=installation,
                bitbucket_workspace_uuid=remote_workspace,
                token=cipher.encrypt(
                    "shared-token", context=context, purpose=CredentialPurpose.TOKEN
                ),
            )
            repository_id, created = await store.bind_repository(
                workspace_id=workspace,
                installation_id=installation,
                repository_uuid=remote_repo,
                full_name="nvdovnar/private",
                default_branch="main",
                web_url="https://bitbucket.org/nvdovnar/private",
            )
            assert created
            await store.create_connection(
                workspace_id=other_workspace,
                installation_id=other_installation,
                bitbucket_workspace_uuid=remote_workspace,
                token=cipher.encrypt(
                    "other-token",
                    context=CredentialContext(other_installation, other_workspace),
                    purpose=CredentialPurpose.TOKEN,
                ),
            )
            other_repository_id, other_created = await store.bind_repository(
                workspace_id=other_workspace,
                installation_id=other_installation,
                repository_uuid=remote_repo,
                full_name="other/private",
                default_branch="main",
                web_url="https://bitbucket.org/other/private",
            )
            assert other_created
            return repository_id, other_repository_id

    repository_id, other_repository_id = asyncio.run(setup())
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    try:
        client = TestClient(app)
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
        visible = client.get("/api/repos")
        assert visible.status_code == 200
        assert [item["id"] for item in visible.json()] == [str(repository_id)]
        assert visible.json()[0]["provider"] == "bitbucket"
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (other_workspace,))
        assert [item["id"] for item in client.get("/api/repos").json()] == [
            str(other_repository_id)
        ]
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
        for body, field in (
            ({"waitForCi": "auto"}, "waitForCi"),
            ({"waitForCi": "always"}, "waitForCi"),
            ({"reviewEvent": "REQUEST_CHANGES"}, "reviewEvent"),
        ):
            rejected = client.patch(f"/api/repos/{repository_id}", json=body)
            assert rejected.status_code == 422
            assert rejected.json()["detail"][0]["loc"] == ["body", field]
        assert tuple(
            db.execute(
                text("SELECT wait_for_ci, review_event FROM repositories WHERE id=:id"),
                {"id": repository_id},
            ).one()
        ) == ("never", "comment")
        accepted = client.patch(
            f"/api/repos/{repository_id}",
            json={"waitForCi": "never", "reviewEvent": "COMMENT"},
        )
        assert accepted.status_code == 200
        assert (accepted.json()["waitForCi"], accepted.json()["reviewEvent"]) == (
            "never",
            "COMMENT",
        )
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(43, (workspace,))
        assert client.get("/api/repos").json() == []
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
        db.execute(
            text(
                "DELETE FROM github_user_workspace_access "
                "WHERE github_user_id=42 AND workspace_id=:workspace"
            ),
            {"workspace": workspace},
        )
        db.commit()
        assert client.get("/api/repos").json() == []
    finally:
        app.dependency_overrides.clear()
        del app.state.reviews_api_resources
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_browser_connect_reuses_one_credential_and_disconnects_one_repository(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, remote_workspace, first_uuid, second_uuid = uuid4(), uuid4(), uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": workspace},
    )
    db.execute(
        text("INSERT INTO github_user_profiles (id, login, name) VALUES (42, 'alice', 'Alice')")
    )
    db.execute(
        text(
            "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
            "VALUES (42, :workspace)"
        ),
        {"workspace": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    rotated_repositories: list[str] = []
    probe_requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] in {
            "Bearer shared-token",
            "Bearer different-token",
            "Bearer rotated-token",
            "Bearer transferred-token",
            "Bearer repository-only-token",
        }
        if request.url.path.endswith("/pullrequests"):
            probe_requests.append(str(request.url))
            if request.headers["Authorization"] == "Bearer repository-only-token":
                return httpx.Response(403, json={"error": {"message": "scope denied"}})
            return httpx.Response(200, json={"values": [], "size": 0, "pagelen": 1})
        if request.url.path == "/2.0/repositories/other-space/first":
            return httpx.Response(
                200,
                json={
                    "uuid": "{" + str(first_uuid) + "}",
                    "full_name": "other-space/first",
                    "slug": "first",
                    "name": "first",
                    "is_private": True,
                    "workspace": {
                        "uuid": "{d9fb570e-22f0-4d8e-868c-0474ab37ce68}",
                        "slug": "other-space",
                    },
                    "links": {"html": {"href": "https://bitbucket.org/other-space/first"}},
                    "mainbranch": {"name": "main"},
                },
            )
        if request.url.path == "/2.0/repositories/nvdovnar":
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "uuid": "{" + str(first_uuid) + "}",
                            "full_name": "nvdovnar/first",
                            "slug": "first",
                            "name": "first",
                            "is_private": True,
                            "workspace": {
                                "uuid": "{" + str(remote_workspace) + "}",
                                "slug": "nvdovnar",
                            },
                            "links": {"html": {"href": "https://bitbucket.org/nvdovnar/first"}},
                            "mainbranch": {"name": "main"},
                            "language": "Python",
                        }
                    ]
                },
            )
        slug = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith(f"/2.0/repositories/{{{remote_workspace}}}/"):
            remote_uuid = UUID(slug.strip("{}"))
            assert remote_uuid in {first_uuid, second_uuid}
            slug = "first-renamed" if remote_uuid == first_uuid else "second-renamed"
            if request.headers["Authorization"] == "Bearer rotated-token":
                rotated_repositories.append(slug)
        elif request.headers["Authorization"] == "Bearer rotated-token":
            assert slug == "first-renamed"
            remote_uuid = first_uuid
        else:
            assert slug in {"first", "second"}
            remote_uuid = first_uuid if slug == "first" else second_uuid
        returned_workspace = (
            UUID("d9fb570e-22f0-4d8e-868c-0474ab37ce68")
            if request.headers["Authorization"] == "Bearer transferred-token"
            else remote_workspace
        )
        return httpx.Response(
            200,
            json={
                "uuid": "{" + str(remote_uuid) + "}",
                "full_name": f"nvdovnar/{slug}",
                "slug": slug,
                "name": slug,
                "is_private": True,
                "workspace": {"uuid": "{" + str(returned_workspace) + "}", "slug": "nvdovnar"},
                "links": {"html": {"href": f"https://bitbucket.org/nvdovnar/{slug}"}},
                "mainbranch": {"name": "main"},
                "language": "Python",
            },
        )

    class RacingProvider(BitbucketCloudRepositoryProvider):
        race_target: tuple[UUID, UUID] | None = None

        async def fetch_repository_by_uuid(
            self, *, repository_uuid: UUID, token: str, workspace_uuid: UUID | None = None
        ) -> BitbucketRepositorySnapshot:
            if self.race_target is not None:
                installation_id, repository_id = self.race_target
                self.race_target = None
                async with factory() as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    assert await store.disconnect_repository(
                        workspace_id=workspace,
                        installation_id=installation_id,
                        repository_id=repository_id,
                    )
                    rebound, newly_active = await store.bind_repository(
                        workspace_id=workspace,
                        installation_id=installation_id,
                        repository_uuid=first_uuid,
                        full_name="nvdovnar/first",
                        default_branch="main",
                        web_url="https://bitbucket.org/nvdovnar/first",
                    )
                    assert rebound == repository_id and newly_active
                    await store.set_repository_webhook_secret(
                        workspace_id=workspace,
                        installation_id=installation_id,
                        repository_id=repository_id,
                        secret=BitbucketCredentialCipher(
                            active_key_id="test", keys={"test": b"a" * 32}
                        ).encrypt(
                            "r" * 40,
                            context=CredentialContext(installation_id, workspace, repository_id),
                            purpose=CredentialPurpose.WEBHOOK_SECRET,
                        ),
                    )
                    await session.commit()
            return await super().fetch_repository_by_uuid(
                repository_uuid=repository_uuid, token=token, workspace_uuid=workspace_uuid
            )

    provider_http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    racing_provider = RacingProvider(provider_http)
    manager = ManageBitbucketConnection(
        uow_factory=lambda: SqlAlchemyBitbucketManagementUnitOfWork(factory),
        cipher=BitbucketCredentialCodecAdapter(
            BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
        ),
        provider=racing_provider,
        rule_sets=load_default_rule_sets(Path(__file__).resolve().parents[1] / "review" / "rules"),
        webhook_base_url="https://portal.example",
    )
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace,))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        client = TestClient(app)
        url = f"/api/workspaces/{workspace}/bitbucket/connections"
        discovery = client.post(
            f"/api/workspaces/{workspace}/bitbucket/discovery",
            json={"bitbucketWorkspace": "nvdovnar", "apiToken": "shared-token"},
        )
        assert discovery.status_code == 200
        assert discovery.json()["repositories"][0]["uuid"] == str(first_uuid)
        assert discovery.json()["repositories"][0]["isPrivate"] is True
        assert "shared-token" not in discovery.text
        first = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "shared-token",
            },
        )
        assert first.status_code == 201
        assert first.json()["repositoryUuid"] == str(first_uuid)
        assert first.json()["webhook"]["secret"]
        second = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "second",
                "repositoryUuid": str(second_uuid),
                "apiToken": "shared-token",
            },
        )
        assert second.status_code == 201
        missing_pr_scope_on_connect = client.post(
            url,
            json={
                "bitbucketWorkspace": "other-space",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "repository-only-token",
            },
        )
        assert missing_pr_scope_on_connect.status_code == 422
        assert missing_pr_scope_on_connect.json() == {"detail": "repository_inaccessible"}
        other_workspace_same_repository = client.post(
            url,
            json={
                "bitbucketWorkspace": "other-space",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "shared-token",
            },
        )
        assert other_workspace_same_repository.status_code == 409
        assert other_workspace_same_repository.json() == {"detail": "repository_uuid_conflict"}
        assert len(client.get(url).json()["connections"]) == 2
        assert len(probe_requests) == 4
        assert all("pullrequests?pagelen=1" in request for request in probe_requests)
        missing_pr_scope = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "repository-only-token"},
        )
        assert missing_pr_scope.status_code == 422
        assert missing_pr_scope.json() == {"detail": "repository_inaccessible"}
        assert client.get(url).json()["connections"][0]["credentialGeneration"] == 1
        transferred = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "transferred-token"},
        )
        assert transferred.status_code == 409
        assert transferred.json() == {"detail": "repository_uuid_conflict"}
        assert client.get(url).json()["connections"][0]["credentialGeneration"] == 1
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["webhook"]["secret"]
        assert second.json()["webhook"]["secret"] != first.json()["webhook"]["secret"]
        for connected in (first, second):
            assert (
                db.scalar(
                    text(
                        "SELECT refresh_requested_at FROM repository_rules_status "
                        "WHERE repository_id=:id"
                    ),
                    {"id": connected.json()["repositoryId"]},
                )
                is not None
            )
        assert first.json()["repositoryId"] in first.json()["webhook"]["url"]
        assert second.json()["repositoryId"] in second.json()["webhook"]["url"]
        first_secret_before = db.scalar(
            text(
                "SELECT secret_ciphertext FROM bitbucket_repository_webhook_secrets "
                "WHERE repository_id=:id"
            ),
            {"id": first.json()["repositoryId"]},
        )
        assert first_secret_before is not None
        rotated_hook = client.post(
            f"{url}/{second.json()['id']}/repositories/"
            f"{second.json()['repositoryId']}/webhook-secret"
        )
        assert rotated_hook.status_code == 200
        assert rotated_hook.json()["secret"] != second.json()["webhook"]["secret"]
        assert (
            db.scalar(
                text(
                    "SELECT secret_ciphertext FROM bitbucket_repository_webhook_secrets "
                    "WHERE repository_id=:id"
                ),
                {"id": first.json()["repositoryId"]},
            )
            == first_secret_before
        )
        racing_provider.race_target = (UUID(first.json()["id"]), UUID(first.json()["repositoryId"]))
        stale_rotation = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "rotated-token"},
        )
        assert stale_rotation.status_code == 409
        assert stale_rotation.json()["detail"] == "connection_changed"
        rotated_credential = client.put(
            f"{url}/{first.json()['id']}/credential",
            json={"apiToken": "rotated-token"},
        )
        assert rotated_credential.status_code == 200
        assert rotated_credential.json()["credentialGeneration"] == 2
        assert set(rotated_repositories) == {"first-renamed", "second-renamed"}
        assert {item["repositorySlug"] for item in client.get(url).json()["connections"]} == {
            "first-renamed",
            "second-renamed",
        }
        assert {item["repositoryId"] for item in client.get(url).json()["connections"]} == {
            first.json()["repositoryId"],
            second.json()["repositoryId"],
        }
        assert {item["id"] for item in client.get("/api/repos").json()} == {
            first.json()["repositoryId"],
            second.json()["repositoryId"],
        }
        target_reader = SqlAlchemyBitbucketTargetReader(factory)
        sibling_target = asyncio.run(
            target_reader.resolve_target(
                workspace_id=workspace,
                installation_id=UUID(first.json()["id"]),
                repository_uuid=second_uuid,
            )
        )
        disconnected_target = asyncio.run(
            target_reader.resolve_target(
                workspace_id=workspace,
                installation_id=UUID(first.json()["id"]),
                repository_uuid=first_uuid,
            )
        )
        disconnect = client.delete(
            f"{url}/{first.json()['id']}/repositories/{first.json()['repositoryId']}"
        )
        assert disconnect.status_code == 204
        assert [item["repositoryId"] for item in client.get(url).json()["connections"]] == [
            second.json()["repositoryId"]
        ]
        assert [item["id"] for item in client.get("/api/repos").json()] == [
            second.json()["repositoryId"]
        ]
        asyncio.run(target_reader.checkpoint(sibling_target))
        with pytest.raises(BitbucketTargetInactive):
            asyncio.run(target_reader.checkpoint(disconnected_target))
        assert (
            db.scalar(
                text(
                    "SELECT secret_ciphertext FROM bitbucket_repository_webhook_secrets "
                    "WHERE repository_id=:id"
                ),
                {"id": first.json()["repositoryId"]},
            )
            is None
        )
        conflicting = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "first",
                "repositoryUuid": str(first_uuid),
                "apiToken": "different-token",
            },
        )
        assert conflicting.status_code == 409
        assert (
            client.delete(
                f"{url}/{second.json()['id']}/repositories/{second.json()['repositoryId']}"
            ).status_code
            == 204
        )
        db.execute(
            text(
                "UPDATE repository_rules_status SET refresh_requested_at=NULL "
                "WHERE repository_id=:id"
            ),
            {"id": first.json()["repositoryId"]},
        )
        db.commit()
        reconnected = client.post(
            url,
            json={
                "bitbucketWorkspace": "nvdovnar",
                "repositorySlug": "first-renamed",
                "repositoryUuid": str(first_uuid),
                "apiToken": "rotated-token",
            },
        )
        assert reconnected.status_code == 201
        assert reconnected.json()["id"] == first.json()["id"]
        assert reconnected.json()["webhook"]["secret"] != first.json()["webhook"]["secret"]
        assert (
            db.scalar(
                text(
                    "SELECT refresh_requested_at FROM repository_rules_status "
                    "WHERE repository_id=:id"
                ),
                {"id": first.json()["repositoryId"]},
            )
            is not None
        )
        db.execute(
            text(
                "DELETE FROM github_user_workspace_access "
                "WHERE github_user_id=42 AND workspace_id=:workspace"
            ),
            {"workspace": workspace},
        )
        db.commit()
        assert client.get(url).status_code == 404
        assert (
            client.post(
                f"/api/workspaces/{workspace}/bitbucket/discovery",
                json={"bitbucketWorkspace": "nvdovnar", "apiToken": "shared-token"},
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"{url}/{first.json()['id']}/credential",
                json={"apiToken": "rotated-token"},
            ).status_code
            == 404
        )
    finally:
        app.dependency_overrides.clear()
        del app.state.reviews_api_resources
        asyncio.run(provider_http.aclose())
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_request_target_is_fenced_after_rotation_and_revocation(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, repository_id = uuid4(), uuid4(), uuid4()
    remote_workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    remote_repo = UUID("3791f4cc-f59a-4412-9a52-f34a7829ba1c")
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": str(workspace)},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    context = CredentialContext(installation, workspace)

    async def scenario() -> None:
        database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg_async"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET search_path TO "{schema}"'))
                await connection.commit()
                async with AsyncSession(bind=connection) as session:
                    store = SqlAlchemyBitbucketConnectionStore(session)
                    await store.create_connection(
                        workspace_id=workspace,
                        installation_id=installation,
                        bitbucket_workspace_uuid=remote_workspace,
                        token=cipher.encrypt(
                            "original-token", context=context, purpose=CredentialPurpose.TOKEN
                        ),
                    )
                    external_id = await session.scalar(
                        select(bitbucket_repository_external_id_seq.next_value())
                    )
                    session.add(
                        Repository(
                            id=repository_id,
                            provider_installation_id=installation,
                            external_id=external_id,
                            full_name="nvdovnar/test-ai-review",
                            default_branch="main",
                            web_url="https://bitbucket.org/nvdovnar/test-ai-review",
                        )
                    )
                    await session.flush()
                    session.add(
                        BitbucketRepositoryIdentity(
                            repository_id=repository_id,
                            provider_installation_id=installation,
                            workspace_id=workspace,
                            repository_external_id=external_id,
                            repository_uuid=remote_repo,
                        )
                    )
                    session.add(BitbucketRepositoryWebhookSecret(repository_id=repository_id))
                    await session.commit()
                    reader = SqlAlchemyBitbucketTargetReader(lambda: AsyncSession(bind=connection))
                    authority = BitbucketRequestCredentials(reader, cipher)
                    target = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=remote_repo,
                    )
                    assert (await authority.credential_for(target)).token == "original-token"
                    assert not connection.in_transaction()
                    sent: list[str] = []

                    def provider(request: httpx.Request) -> httpx.Response:
                        assert not connection.in_transaction()
                        sent.append(str(request.url))
                        return httpx.Response(200)

                    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
                        response = await BitbucketHttpClient(http, authority).get(
                            target, "/2.0/repositories/nvdovnar/test-ai-review"
                        )
                        assert response.status_code == 200
                    assert sent == [
                        "https://api.bitbucket.org/2.0/repositories/nvdovnar/test-ai-review"
                    ]
                    await store.rotate_token(
                        workspace_id=workspace,
                        installation_id=installation,
                        token=cipher.encrypt(
                            "rotated-token", context=context, purpose=CredentialPurpose.TOKEN
                        ),
                    )
                    await session.commit()
                    with pytest.raises(BitbucketCredentialGenerationChanged):
                        await authority.credential_for(target)
                    fresh = await reader.resolve_target(
                        workspace_id=workspace,
                        installation_id=installation,
                        repository_uuid=remote_repo,
                    )
                    assert (await authority.credential_for(fresh)).token == "rotated-token"
                    assert not connection.in_transaction()
                    await store.revoke_connection(
                        workspace_id=workspace, installation_id=installation
                    )
                    await session.commit()
                    with pytest.raises(BitbucketTargetInactive):
                        await reader.checkpoint(fresh)
                    with pytest.raises(BitbucketTargetInactive):
                        await reader.checkpoint_in_session(session, fresh)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
