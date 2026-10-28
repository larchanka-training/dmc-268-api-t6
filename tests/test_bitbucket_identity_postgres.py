"""Bitbucket UUID identity at the migrated PostgreSQL boundary (#333)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.db_metadata import metadata
from app.modules.code_index.infrastructure.effective_rag import INDEXABLE_PROVIDER, RAG_ON
from app.modules.integrations.bitbucket.infrastructure.identity_store import (
    resolve_bitbucket_repository,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository

REPO_UUID = "3791f4cc-f59a-4412-9a52-f34a7829ba1c"
BB_WORKSPACE_UUID = "b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60"
OTHER_BB_WORKSPACE_UUID = "ac58dbe0-d167-412b-90df-610c7ef8cad9"


@pytest.fixture
def bitbucket_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL identity tests")
    schema = f"test_bitbucket_identity_{uuid4().hex}"
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


def _workspace(connection: Connection, name: str) -> str:
    workspace_id = str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
        {"id": workspace_id, "name": name},
    )
    return workspace_id


def _installation(connection: Connection, workspace_id: str, provider: str) -> str:
    installation_id = str(uuid4())
    external_id = connection.scalar(
        text("SELECT nextval('bitbucket_installation_external_id_seq')")
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, :provider, :external, '{}')"
        ),
        {
            "id": installation_id,
            "workspace": workspace_id,
            "provider": provider,
            "external": external_id,
        },
    )
    return installation_id


def _bind_installation(
    connection: Connection,
    workspace_id: str,
    installation_id: str,
    bitbucket_workspace_uuid: str = BB_WORKSPACE_UUID,
) -> None:
    connection.execute(
        text(
            "INSERT INTO bitbucket_installation_identities "
            "(provider_installation_id, workspace_id, bitbucket_workspace_uuid) "
            "VALUES (:installation, :workspace, :uuid)"
        ),
        {
            "installation": installation_id,
            "workspace": workspace_id,
            "uuid": bitbucket_workspace_uuid,
        },
    )


def _repository(
    connection: Connection, workspace_id: str, installation_id: str, repo_uuid: str, slug: str
) -> str:
    repository_id = str(uuid4())
    external_id = connection.scalar(text("SELECT nextval('bitbucket_repository_external_id_seq')"))
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, :external, :slug, 'main', :url)"
        ),
        {
            "id": repository_id,
            "installation": installation_id,
            "external": external_id,
            "slug": slug,
            "url": f"https://bitbucket.org/{slug}",
        },
    )
    connection.execute(
        text(
            "INSERT INTO bitbucket_repository_identities "
            "(repository_id, provider_installation_id, workspace_id, "
            "repository_external_id, repository_uuid) "
            "VALUES (:repo, :installation, :workspace, :external, :uuid)"
        ),
        {
            "repo": repository_id,
            "installation": installation_id,
            "workspace": workspace_id,
            "external": external_id,
            "uuid": repo_uuid,
        },
    )
    return repository_id


@pytest.mark.integration
def test_bitbucket_uuid_survives_rename_and_two_workspace_connections_are_independent(
    bitbucket_schema: Connection,
) -> None:
    db = bitbucket_schema
    command.upgrade(_config(db), "head")
    first_workspace = _workspace(db, "first")
    second_workspace = _workspace(db, "second")
    first_installation = _installation(db, first_workspace, "bitbucket")
    second_installation = _installation(db, second_workspace, "bitbucket")
    other_installation = _installation(db, first_workspace, "bitbucket")
    _bind_installation(db, first_workspace, first_installation)
    _bind_installation(db, second_workspace, second_installation)
    _bind_installation(db, first_workspace, other_installation, OTHER_BB_WORKSPACE_UUID)
    first_repo = _repository(
        db, first_workspace, first_installation, REPO_UUID, "nvdovnar/test-ai-review"
    )
    second_repo = _repository(
        db, second_workspace, second_installation, REPO_UUID, "nvdovnar/test-ai-review"
    )
    db.execute(
        text("UPDATE repositories SET full_name='renamed/repo' WHERE id=:id"),
        {"id": first_repo},
    )

    def find(workspace: str, installation: str, repo_uuid: str) -> list[tuple[str, str]]:
        rows = db.execute(
            text(
                "SELECT r.id, r.full_name FROM repositories r "
                "JOIN bitbucket_repository_identities b ON b.repository_id=r.id "
                "WHERE b.workspace_id=:workspace AND "
                "b.provider_installation_id=:installation AND b.repository_uuid=:uuid"
            ),
            {"workspace": workspace, "installation": installation, "uuid": repo_uuid},
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    assert find(first_workspace, first_installation, REPO_UUID) == [(first_repo, "renamed/repo")]
    assert find(first_workspace, other_installation, REPO_UUID) == []
    assert find(second_workspace, second_installation, REPO_UUID) == [
        (second_repo, "nvdovnar/test-ai-review")
    ]
    db.execute(
        text("UPDATE provider_installations SET deleted_at=now() WHERE id=:id"),
        {"id": first_installation},
    )
    assert find(second_workspace, second_installation, REPO_UUID) == [
        (second_repo, "nvdovnar/test-ai-review")
    ]


@pytest.mark.integration
def test_bitbucket_uuid_cannot_be_claimed_twice_or_rebound_to_another_owner(
    bitbucket_schema: Connection,
) -> None:
    db = bitbucket_schema
    command.upgrade(_config(db), "head")
    first_workspace = _workspace(db, "first")
    second_workspace = _workspace(db, "second")
    first_installation = _installation(db, first_workspace, "bitbucket")
    second_installation = _installation(db, second_workspace, "bitbucket")
    other_installation = _installation(db, first_workspace, "bitbucket")
    _bind_installation(db, first_workspace, first_installation)
    _bind_installation(db, second_workspace, second_installation)
    _bind_installation(db, first_workspace, other_installation, OTHER_BB_WORKSPACE_UUID)
    repository_id = _repository(db, first_workspace, first_installation, REPO_UUID, "first/test")

    with pytest.raises(IntegrityError), db.begin_nested():
        _repository(db, first_workspace, other_installation, REPO_UUID, "first/duplicate")

    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "UPDATE bitbucket_repository_identities "
                "SET provider_installation_id=:installation, workspace_id=:workspace "
                "WHERE repository_id=:repo"
            ),
            {
                "repo": repository_id,
                "installation": second_installation,
                "workspace": second_workspace,
            },
        )

    for table, column, id_column, row_id in (
        (
            "bitbucket_installation_identities",
            "bitbucket_workspace_uuid",
            "provider_installation_id",
            first_installation,
        ),
        ("bitbucket_repository_identities", "repository_uuid", "repository_id", repository_id),
    ):
        with pytest.raises(IntegrityError) as error, db.begin_nested():
            db.execute(
                text(f"UPDATE {table} SET {column}=:new_uuid WHERE {id_column}=:id"),
                {"new_uuid": str(uuid4()), "id": row_id},
            )
        assert getattr(error.value.orig, "sqlstate", None) == "23514"


@pytest.mark.integration
def test_bitbucket_receipt_requires_both_generations(bitbucket_schema: Connection) -> None:
    db = bitbucket_schema
    command.upgrade(_config(db), "head")
    workspace = _workspace(db, "first")
    installation = _installation(db, workspace, "bitbucket")
    _bind_installation(db, workspace, installation)
    repository = _repository(db, workspace, installation, REPO_UUID, "first/repo")
    for binding, webhook in ((None, 1), (1, None)):
        with pytest.raises(IntegrityError) as error, db.begin_nested():
            db.execute(
                text(
                    "INSERT INTO webhook_events "
                    "(id, provider, provider_installation_id, bitbucket_repository_id, "
                    "bitbucket_binding_generation, bitbucket_webhook_generation, "
                    "delivery_id, event, payload) "
                    "VALUES (:id, 'bitbucket', :installation, :repository, :binding, "
                    ":webhook, :delivery, 'repo:push', '{}'::jsonb)"
                ),
                {
                    "id": str(uuid4()),
                    "installation": installation,
                    "repository": repository,
                    "binding": binding,
                    "webhook": webhook,
                    "delivery": str(uuid4()),
                },
            )
        assert getattr(error.value.orig, "sqlstate", None) == "23514"


@pytest.mark.integration
def test_indexable_bitbucket_requires_live_webhook_secret_and_matching_workspace_joins(
    bitbucket_schema: Connection,
) -> None:
    db = bitbucket_schema
    command.upgrade(_config(db), "head")
    workspace = _workspace(db, "first")
    installation = _installation(db, workspace, "bitbucket")
    _bind_installation(db, workspace, installation)
    repository = _repository(db, workspace, installation, REPO_UUID, "first/repo")
    db.execute(text("UPDATE repositories SET rag_enabled=true WHERE id=:id"), {"id": repository})
    db.execute(
        text(
            "INSERT INTO bitbucket_connections "
            "(provider_installation_id, workspace_id, token_ciphertext, token_key_id) "
            "VALUES (:installation, :workspace, :ciphertext, 'test')"
        ),
        {"installation": installation, "workspace": workspace, "ciphertext": b"encrypted"},
    )
    db.execute(
        text("INSERT INTO bitbucket_repository_webhook_secrets (repository_id) VALUES (:id)"),
        {"id": repository},
    )

    def indexable() -> bool:
        return (
            db.scalar(
                select(Repository.id)
                .join(
                    ProviderInstallation,
                    ProviderInstallation.id == Repository.provider_installation_id,
                )
                .where(Repository.id == UUID(repository), RAG_ON, INDEXABLE_PROVIDER)
            )
            is not None
        )

    assert indexable()
    db.execute(
        text(
            "UPDATE bitbucket_repository_webhook_secrets SET revoked_at=now() "
            "WHERE repository_id=:id"
        ),
        {"id": repository},
    )
    assert not indexable()
    db.execute(
        text(
            "UPDATE bitbucket_repository_webhook_secrets SET revoked_at=NULL "
            "WHERE repository_id=:id"
        ),
        {"id": repository},
    )
    assert indexable()

    # Foreign keys normally prohibit these cross-workspace rows. Shadow the two inner
    # tables without foreign keys to prove each query predicate independently.
    schema = str(db.scalar(text("SELECT current_schema()")))
    for table in ("bitbucket_connections", "bitbucket_repository_identities"):
        db.execute(text(f'CREATE TEMP TABLE {table} AS SELECT * FROM "{schema}".{table}'))
    assert indexable()
    other_workspace = str(uuid4())
    for table, id_column in (
        ("bitbucket_connections", "provider_installation_id"),
        ("bitbucket_repository_identities", "repository_id"),
    ):
        row_id = installation if table == "bitbucket_connections" else repository
        db.execute(
            text(f"UPDATE pg_temp.{table} SET workspace_id=:workspace WHERE {id_column}=:id"),
            {"workspace": other_workspace, "id": row_id},
        )
        assert not indexable()
        db.execute(
            text(f"UPDATE pg_temp.{table} SET workspace_id=:workspace WHERE {id_column}=:id"),
            {"workspace": workspace, "id": row_id},
        )
        assert indexable()


@pytest.mark.integration
def test_lookup_requires_active_workspace_and_installation(bitbucket_schema: Connection) -> None:
    db = bitbucket_schema
    command.upgrade(_config(db), "head")
    first_workspace = _workspace(db, "first")
    second_workspace = _workspace(db, "second")
    first_installation = _installation(db, first_workspace, "bitbucket")
    second_installation = _installation(db, second_workspace, "bitbucket")
    _bind_installation(db, first_workspace, first_installation)
    _bind_installation(db, second_workspace, second_installation)
    first_repo = _repository(db, first_workspace, first_installation, REPO_UUID, "first/repo")
    second_repo = _repository(db, second_workspace, second_installation, REPO_UUID, "second/repo")
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()

    async def check(*, first_active: bool) -> None:
        database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
            drivername="postgresql+psycopg_async"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET search_path TO "{schema}"'))
                async with AsyncSession(bind=connection) as session:
                    uuid = UUID(REPO_UUID)
                    first = UUID(first_workspace)
                    second = UUID(second_workspace)
                    first_origin = UUID(first_installation)
                    second_origin = UUID(second_installation)
                    assert await resolve_bitbucket_repository(
                        session,
                        workspace_id=first,
                        installation_id=first_origin,
                        repository_uuid=uuid,
                    ) == (UUID(first_repo) if first_active else None)
                    assert await resolve_bitbucket_repository(
                        session,
                        workspace_id=second,
                        installation_id=second_origin,
                        repository_uuid=uuid,
                    ) == UUID(second_repo)
                    assert (
                        await resolve_bitbucket_repository(
                            session,
                            workspace_id=first,
                            installation_id=second_origin,
                            repository_uuid=uuid,
                        )
                        is None
                    )
                    assert (
                        await resolve_bitbucket_repository(
                            session,
                            workspace_id=second,
                            installation_id=first_origin,
                            repository_uuid=uuid,
                        )
                        is None
                    )
        finally:
            await engine.dispose()

    asyncio.run(check(first_active=True))
    db.execute(
        text("UPDATE provider_installations SET deleted_at=now() WHERE id=:id"),
        {"id": first_installation},
    )
    db.commit()
    asyncio.run(check(first_active=False))


@pytest.mark.integration
def test_migration_preserves_existing_github_and_gitlab_identity(
    bitbucket_schema: Connection,
) -> None:
    db = bitbucket_schema
    command.upgrade(_config(db), "20261026_0042")
    workspace = _workspace(db, "existing")
    old = []
    for provider, external in (("github", 51), ("gitlab", 52)):
        installation_id = str(uuid4())
        db.execute(
            text(
                "INSERT INTO provider_installations "
                "(id, workspace_id, provider, external_id, metadata) "
                "VALUES (:id, :workspace, :provider, :external, '{}')"
            ),
            {
                "id": installation_id,
                "workspace": workspace,
                "provider": provider,
                "external": external,
            },
        )
        repository_id = str(uuid4())
        db.execute(
            text(
                "INSERT INTO repositories "
                "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
                "VALUES (:id, :installation, :external, :name, 'main', :url)"
            ),
            {
                "id": repository_id,
                "installation": installation_id,
                "external": external + 100,
                "name": f"{provider}/repo",
                "url": f"https://example.com/{provider}/repo",
            },
        )
        old.append((provider, external, repository_id, external + 100))
    command.upgrade(_config(db), "head")
    actual = db.execute(
        text(
            "SELECT i.provider, i.external_id, r.id, r.external_id "
            "FROM repositories r JOIN provider_installations i ON i.id=r.provider_installation_id "
            "ORDER BY i.provider"
        )
    ).all()
    assert [(p, i, str(r), e) for p, i, r, e in actual] == old


@pytest.mark.integration
def test_migrated_identity_tables_match_orm_metadata(bitbucket_schema: Connection) -> None:
    db = bitbucket_schema
    command.upgrade(_config(db), "head")
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
def test_downgrade_refuses_to_drop_bound_bitbucket_identity(
    bitbucket_schema: Connection,
) -> None:
    db = bitbucket_schema
    config = _config(db)
    command.upgrade(config, "head")
    workspace = _workspace(db, "owner")
    installation = _installation(db, workspace, "bitbucket")
    _bind_installation(db, workspace, installation)
    _repository(db, workspace, installation, REPO_UUID, "owner/test")
    db.commit()
    with pytest.raises(RuntimeError, match="Bitbucket identity"):
        command.downgrade(config, "20261026_0042")
    db.rollback()
    assert db.scalar(text("SELECT count(*) FROM bitbucket_repository_identities")) == 1
