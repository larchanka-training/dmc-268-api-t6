"""Digest-only authentication against synthetic migrated PostgreSQL connections."""

from __future__ import annotations

import asyncio
import os
import traceback
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.gitlab import GitLabWebhookUnavailable, build_gitlab_webhook_resources
from app.modules.integrations.gitlab.application.authenticate_webhook import (
    AuthenticateGitLabWebhook,
    GitLabWebhookAuthStatus,
)
from app.modules.integrations.gitlab.application.webhook_receipts import prepare_gitlab_receipt
from app.modules.integrations.gitlab.infrastructure.credential_cipher import digest_webhook_secret
from app.modules.integrations.gitlab.infrastructure.webhook_auth import (
    GitLabWebhookSecretVerifierAdapter,
    SqlAlchemyGitLabWebhookAuthReader,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)

pytestmark = pytest.mark.integration
CONNECTION = UUID("00000000-0000-0000-0000-000000000071")
OTHER = UUID("00000000-0000-0000-0000-000000000072")
GITHUB = UUID("00000000-0000-0000-0000-000000000073")
UNKNOWN = UUID("00000000-0000-0000-0000-000000000074")
SECRET = "synthetic-webhook-secret-canary-0001"
OTHER_SECRET = "synthetic-webhook-secret-canary-0002"


@dataclass(frozen=True)
class Database:
    url: str = field(repr=False)
    schema: str

    @asynccontextmanager
    async def sessions(self) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
        engine = create_async_engine(
            self.url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_webhook_auth_{uuid4().hex}"
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for number, origin, secret, provider in (
                (71, CONNECTION, SECRET, "gitlab"),
                (72, OTHER, OTHER_SECRET, "gitlab"),
                (71, GITHUB, SECRET, "github"),
            ):
                workspace = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'T7a', 7)"
                    ),
                    {"id": workspace},
                )
                connection.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id, workspace_id, provider, external_id, metadata) "
                        "VALUES (:id, :workspace, :provider, :number, '{}')"
                    ),
                    {"id": origin, "workspace": workspace, "number": number, "provider": provider},
                )
                if provider == "gitlab":
                    instance = f"https://gitlab{number}.example"
                    connection.execute(
                        text(
                            "INSERT INTO gitlab_connections "
                            "(provider_installation_id, workspace_id, instance_origin, "
                            "token_ciphertext, credential_key_id, webhook_secret_digest, "
                            "intake_enabled) VALUES (:id, :workspace, :instance, "
                            ":envelope, 'missing-pat-key', :digest, true)"
                        ),
                        {
                            "id": origin,
                            "workspace": workspace,
                            "instance": instance,
                            "envelope": b"synthetic-pat-envelope-canary",
                            "digest": digest_webhook_secret(secret),
                        },
                    )
                    repository = uuid4()
                    connection.execute(
                        text(
                            "INSERT INTO repositories (id, provider_installation_id, "
                            "external_id, full_name, default_branch, web_url) VALUES "
                            "(:id, :origin, 17, 'team/repo', 'main', :url)"
                        ),
                        {"id": repository, "origin": origin, "url": instance + "/team/repo"},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO gitlab_project_bindings (repository_id, "
                            "provider_installation_id, instance_origin, project_external_id) "
                            "VALUES (:id, :origin, :instance, 17)"
                        ),
                        {"id": repository, "origin": origin, "instance": instance},
                    )
            connection.commit()
        yield Database(url, schema)
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


def authenticator(sessions: async_sessionmaker[AsyncSession]) -> AuthenticateGitLabWebhook:
    return AuthenticateGitLabWebhook(
        reader=SqlAlchemyGitLabWebhookAuthReader(sessions),
        verifier=GitLabWebhookSecretVerifierAdapter(),
    )


def test_authenticates_persisted_digest_without_pat_configuration(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            assert (
                await authenticator(sessions).execute(connection_id=CONNECTION, secret=SECRET)
                == GitLabWebhookAuthStatus.AUTHENTICATED
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["deleted", "revoked", "revoked_without_credentials"])
def test_inactive_credentials_fail_uniformly(database: Database, state: str) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            changes = {
                "deleted": "UPDATE provider_installations SET deleted_at=now() WHERE id=:id",
                "revoked": "UPDATE gitlab_connections SET revoked_at=now(), intake_enabled=false "
                "WHERE provider_installation_id=:id",
                "revoked_without_credentials": "UPDATE gitlab_connections SET revoked_at=now(), "
                "intake_enabled=false, token_ciphertext=NULL, credential_key_id=NULL "
                "WHERE provider_installation_id=:id",
            }
            async with sessions() as session:
                await session.execute(text(changes[state]), {"id": CONNECTION})
                await session.commit()
            assert (
                await authenticator(sessions).execute(connection_id=CONNECTION, secret=SECRET)
                == GitLabWebhookAuthStatus.INVALID_CREDENTIALS
            )
            assert (
                await authenticator(sessions).execute(connection_id=OTHER, secret=OTHER_SECRET)
                == GitLabWebhookAuthStatus.AUTHENTICATED
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("connection_id", "secret"),
    [
        (GITHUB, SECRET),
        (UNKNOWN, SECRET),
        (CONNECTION, OTHER_SECRET),
        (OTHER, SECRET),
        (CONNECTION, None),
        (CONNECTION, "short"),
        (CONNECTION, " " * 32),
        (CONNECTION, "é" * 32),
        (CONNECTION, "x" * 257),
    ],
)
def test_provider_origin_and_secret_fences(
    database: Database, connection_id: UUID, secret: str | None
) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            assert (
                await authenticator(sessions).execute(connection_id=connection_id, secret=secret)
                == GitLabWebhookAuthStatus.INVALID_CREDENTIALS
            )

    asyncio.run(scenario())


def test_disabled_state_requires_authentication(database: Database) -> None:
    async def scenario() -> None:
        async with database.sessions() as sessions:
            async with sessions() as session:
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET intake_enabled=false "
                        "WHERE provider_installation_id=:id"
                    ),
                    {"id": CONNECTION},
                )
                await session.commit()
            auth = build_gitlab_webhook_resources(sessions, intake_enabled=True)
            assert await auth.authenticate(connection_id=CONNECTION, secret=OTHER_SECRET) == (
                GitLabWebhookAuthStatus.INVALID_CREDENTIALS
            )
            assert await auth.authenticate(connection_id=CONNECTION, secret=SECRET) == (
                GitLabWebhookAuthStatus.DISABLED
            )

    asyncio.run(scenario())


def test_enabled_resources_use_shared_pool_and_commit_receipts(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
        "SSL_CERT_FILE",
        "GITLAB_TOKEN",
        "GITLAB_BASE_URL",
    ):
        monkeypatch.setenv(name, "invalid-configuration-canary")

    async def scenario() -> None:
        async with database.sessions() as sessions:
            resources = build_gitlab_webhook_resources(sessions, intake_enabled=True)
            assert await resources.authenticate(connection_id=CONNECTION, secret=SECRET) == (
                GitLabWebhookAuthStatus.AUTHENTICATED
            )
            assert await resources.authenticate(connection_id=OTHER, secret=OTHER_SECRET) == (
                GitLabWebhookAuthStatus.AUTHENTICATED
            )
            delivery = prepare_gitlab_receipt(
                connection_id=CONNECTION,
                webhook_id="auth-seam-event",
                event_name="Merge Request Hook",
                payload_json='{"object_kind":"merge_request","project":{"id":17},'
                '"object_attributes":{"action":"open"}}',
            )
            assert await resources.accept(delivery) == InstallationDeliveryDispatchStatus.PENDING
            assert await resources.accept(delivery) == InstallationDeliveryDispatchStatus.DUPLICATE
            async with sessions() as session:
                stored = (
                    await session.execute(
                        text(
                            "SELECT provider, provider_installation_id, "
                            "gitlab_connection_id, installation_external_id FROM webhook_events"
                        )
                    )
                ).one()
                assert tuple(stored) == ("gitlab", CONNECTION, CONNECTION, 71)
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(scenario())


def test_default_factory_makes_no_db_connection(database: Database) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database.url)
        connections: list[bool] = []

        def connected(*args: object) -> None:
            connections.append(True)

        event.listen(engine.sync_engine, "connect", connected)
        try:
            resources = build_gitlab_webhook_resources(async_sessionmaker(engine))
            with pytest.raises(GitLabWebhookUnavailable):
                await resources.authenticate(connection_id=CONNECTION, secret=SECRET)
            delivery = prepare_gitlab_receipt(
                connection_id=CONNECTION,
                webhook_id="off-event",
                event_name="Push Hook",
                payload_json="{}",
            )
            with pytest.raises(GitLabWebhookUnavailable):
                await resources.accept(delivery)
            assert connections == []
            # The borrowed pool remains caller-owned and usable.
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT 7")) == 7
            assert connections == [True]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_detached_read_leaves_no_transaction_or_parent_lock(database: Database) -> None:
    async def scenario() -> None:
        engine = create_async_engine(
            database.url,
            connect_args={
                "options": f"-csearch_path={database.schema}",
                "application_name": database.schema,
            },
        )
        sessions = async_sessionmaker(engine)
        try:
            record = await SqlAlchemyGitLabWebhookAuthReader(sessions).read(CONNECTION)
            assert record is not None
            assert record.connection_id == CONNECTION
            assert record.active is True
            assert SECRET not in repr(record)
            assert digest_webhook_secret(SECRET) not in repr(record)
            assert "synthetic-pat-envelope-canary" not in repr(record)
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:app "
                            "AND state='idle in transaction' AND pid <> pg_backend_pid()"
                        ),
                        {"app": database.schema},
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"
                        ),
                        {"id": CONNECTION},
                    )
                    == CONNECTION
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["authenticate", "accept"])
def test_resources_sanitize_real_sql_failures(
    database: Database,
    operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(
            database.url,
            connect_args={"options": f"-csearch_path={database.schema}_absent"},
            hide_parameters=False,
        )
        try:
            resources = build_gitlab_webhook_resources(
                async_sessionmaker(engine),
                intake_enabled=True,
            )
            with pytest.raises(GitLabWebhookUnavailable) as raised:
                if operation == "authenticate":
                    await resources.authenticate(connection_id=CONNECTION, secret=SECRET)
                else:
                    await resources.accept(
                        prepare_gitlab_receipt(
                            connection_id=CONNECTION,
                            webhook_id="failure-event",
                            event_name="Push Hook",
                            payload_json="{}",
                        )
                    )
            assert str(raised.value) == "gitlab_webhook_unavailable"
            rendered = "".join(traceback.format_exception(raised.value))
            for canary in (SECRET, database.url, "psycopg", "UndefinedTable", "SELECT "):
                assert canary not in rendered
                assert canary not in caplog.text
            assert raised.value.__cause__ is None
            assert raised.value.__suppress_context__ is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())
