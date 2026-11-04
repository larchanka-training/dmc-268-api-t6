"""Bitbucket credential rows keep secret and tenant authority isolated (#333)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from alembic import command
from app.bootstrap.db_metadata import metadata
from app.modules.integrations.bitbucket.application.manage_connection import (
    BindingPosition,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from tests.bitbucket_connection_postgres_support import _config

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


def test_connection_store_exposes_repository_disconnect_without_bulk_revoke() -> None:
    assert not hasattr(SqlAlchemyBitbucketConnectionStore, "revoke_connection")


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
def test_binding_page_migration_backfills_only_bitbucket_workspace(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "20261027_0052")
    workspace_id, bitbucket_installation, github_installation = uuid4(), uuid4(), uuid4()
    bitbucket_repository, github_repository = uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
        {"id": workspace_id, "name": str(workspace_id)},
    )
    for installation, provider, external_id in (
        (bitbucket_installation, "bitbucket", 800001),
        (github_installation, "github", 800002),
    ):
        db.execute(
            text("""
                INSERT INTO provider_installations
                    (id, workspace_id, provider, external_id, "metadata")
                VALUES (:id, :workspace_id, :provider, :external_id, '{}'::jsonb)
            """),
            {
                "id": installation,
                "workspace_id": workspace_id,
                "provider": provider,
                "external_id": external_id,
            },
        )
    db.execute(
        text("""
            INSERT INTO bitbucket_installation_identities
                (provider_installation_id, workspace_id, bitbucket_workspace_uuid)
            VALUES (:installation_id, :workspace_id, :remote_workspace_id)
        """),
        {
            "installation_id": bitbucket_installation,
            "workspace_id": workspace_id,
            "remote_workspace_id": uuid4(),
        },
    )
    for repository, installation, name in (
        (bitbucket_repository, bitbucket_installation, "team/bitbucket"),
        (github_repository, github_installation, "team/github"),
    ):
        db.execute(
            text("""
                INSERT INTO repositories
                    (id, provider_installation_id, external_id, full_name,
                     default_branch, web_url)
                VALUES (:id, :installation_id, 1, :name, 'main', :url)
            """),
            {
                "id": repository,
                "installation_id": installation,
                "name": name,
                "url": f"https://example.test/{name}",
            },
        )
    db.execute(
        text("""
            INSERT INTO bitbucket_repository_identities
                (repository_id, provider_installation_id, workspace_id,
                 repository_external_id, repository_uuid)
            VALUES (:repository_id, :installation_id, :workspace_id, 1, :remote_repository_id)
        """),
        {
            "repository_id": bitbucket_repository,
            "installation_id": bitbucket_installation,
            "workspace_id": workspace_id,
            "remote_repository_id": uuid4(),
        },
    )
    db.commit()
    command.upgrade(_config(db), "head")
    assert (
        db.scalar(
            text("SELECT workspace_id FROM repositories WHERE id = :id"),
            {"id": bitbucket_repository},
        )
        == workspace_id
    )
    assert (
        db.scalar(
            text("SELECT workspace_id FROM repositories WHERE id = :id"),
            {"id": github_repository},
        )
        is None
    )


@pytest.mark.integration
def test_binding_pages_keep_database_collation_order(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    if db.scalar(text("SELECT 1 FROM pg_collation WHERE collname = 'en-US-x-icu'")) is None:
        pytest.skip("PostgreSQL ICU English collation is unavailable")
    db.execute(
        text(
            "ALTER TABLE repositories ALTER COLUMN full_name TYPE VARCHAR(512) "
            'COLLATE "en-US-x-icu"'
        )
    )
    workspace_id, installation_id = uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
        {"id": workspace_id, "name": str(workspace_id)},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
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
                    await store.create_connection(
                        workspace_id=workspace_id,
                        installation_id=installation_id,
                        bitbucket_workspace_uuid=uuid4(),
                        token=cipher.encrypt(
                            "token",
                            context=CredentialContext(installation_id, workspace_id),
                            purpose=CredentialPurpose.TOKEN,
                        ),
                    )
                    for name in ("Team/A", "team/a", "team/á", "team/z", "team/B", "TEAM/c"):
                        await store.bind_repository(
                            workspace_id=workspace_id,
                            installation_id=installation_id,
                            repository_uuid=uuid4(),
                            full_name=name,
                            default_branch="main",
                            web_url=f"https://bitbucket.org/{name}",
                        )
                    await session.commit()
                    expected = (
                        (
                            await session.execute(
                                text("""
                                SELECT full_name FROM repositories
                                WHERE workspace_id = :workspace_id
                                ORDER BY full_name, id
                            """),
                                {"workspace_id": workspace_id},
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert expected != sorted(expected)
                    seen: list[str] = []
                    after: BindingPosition | None = None
                    for _ in range(4):
                        page = await store.list_bindings_page(
                            workspace_id=workspace_id,
                            installation_id=None,
                            after=after,
                            limit=2,
                        )
                        seen.extend(item.full_name for item in page[:2])
                        if len(page) <= 2:
                            break
                        last = page[1]
                        after = BindingPosition(
                            workspace_id,
                            last.installation_id,
                            last.full_name,
                            last.repository_id,
                        )
                    assert seen == expected
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_binding_pages_are_scoped_and_stable_across_writes(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace_id, other_workspace_id = uuid4(), uuid4()
    for identifier in (workspace_id, other_workspace_id):
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
            {"id": identifier, "name": str(identifier)},
        )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    installation_id, second_installation_id, foreign_installation_id = uuid4(), uuid4(), uuid4()
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
                    for tenant, installation in (
                        (workspace_id, installation_id),
                        (workspace_id, second_installation_id),
                        (other_workspace_id, foreign_installation_id),
                    ):
                        await store.create_connection(
                            workspace_id=tenant,
                            installation_id=installation,
                            bitbucket_workspace_uuid=uuid4(),
                            token=cipher.encrypt(
                                "token",
                                context=CredentialContext(installation, tenant),
                                purpose=CredentialPurpose.TOKEN,
                            ),
                        )
                    for name, tenant, installation in (
                        ("team/a", workspace_id, installation_id),
                        ("team/b", workspace_id, second_installation_id),
                        ("team/c", workspace_id, installation_id),
                        ("team/d", workspace_id, installation_id),
                        ("team/private", other_workspace_id, foreign_installation_id),
                    ):
                        await store.bind_repository(
                            workspace_id=tenant,
                            installation_id=installation,
                            repository_uuid=uuid4(),
                            full_name=name,
                            default_branch="main",
                            web_url=f"https://bitbucket.org/{name}",
                        )
                    await session.commit()
                    first = await store.list_bindings_page(
                        workspace_id=workspace_id, installation_id=None, after=None, limit=2
                    )
                    assert [item.full_name for item in first] == ["team/a", "team/b", "team/c"]
                    after = BindingPosition(
                        workspace_id,
                        first[1].installation_id,
                        first[1].full_name,
                        first[1].repository_id,
                    )
                    await store.bind_repository(
                        workspace_id=workspace_id,
                        installation_id=installation_id,
                        repository_uuid=uuid4(),
                        full_name="team/aa",
                        default_branch="main",
                        web_url="https://bitbucket.org/team/aa",
                    )
                    await store.disconnect_repository(
                        workspace_id=workspace_id,
                        installation_id=installation_id,
                        repository_id=first[0].repository_id,
                    )
                    second = await store.list_bindings_page(
                        workspace_id=workspace_id, installation_id=None, after=after, limit=2
                    )
                    assert [item.full_name for item in second] == ["team/c", "team/d"]
                    assert (
                        await store.list_bindings_page(
                            workspace_id=workspace_id,
                            installation_id=None,
                            after=BindingPosition(
                                workspace_id,
                                second[-1].installation_id,
                                second[-1].full_name,
                                second[-1].repository_id,
                            ),
                            limit=2,
                        )
                        == ()
                    )
                    scoped = await store.list_bindings_page(
                        workspace_id=workspace_id,
                        installation_id=installation_id,
                        after=None,
                        limit=2,
                    )
                    assert [item.full_name for item in scoped] == ["team/aa", "team/c", "team/d"]
                    with pytest.raises(ValueError, match="invalid_binding_position"):
                        await store.list_bindings_page(
                            workspace_id=workspace_id,
                            installation_id=None,
                            after=BindingPosition(
                                other_workspace_id, foreign_installation_id, "team/private", uuid4()
                            ),
                            limit=2,
                        )
                    with pytest.raises(ValueError, match="invalid_binding_position"):
                        await store.list_bindings_page(
                            workspace_id=workspace_id,
                            installation_id=installation_id,
                            after=BindingPosition(
                                workspace_id, second_installation_id, "team/b", uuid4()
                            ),
                            limit=2,
                        )
                    for invalid_limit in (0, 101):
                        with pytest.raises(ValueError, match="invalid_binding_position"):
                            await store.list_bindings_page(
                                workspace_id=workspace_id,
                                installation_id=None,
                                after=None,
                                limit=invalid_limit,
                            )
                    other_provider_installation = uuid4()
                    await session.execute(
                        text("""
                            INSERT INTO provider_installations
                                (id, workspace_id, provider, external_id, "metadata")
                            VALUES (:id, :workspace_id, 'github', 900001, '{}'::jsonb)
                        """),
                        {"id": other_provider_installation, "workspace_id": workspace_id},
                    )
                    await session.execute(
                        text("""
                            INSERT INTO repositories
                                (id, provider_installation_id, workspace_id, external_id,
                                 full_name, default_branch, web_url)
                            VALUES (:id, :installation_id, :workspace_id, 900001,
                                    'team/bb', 'main', 'https://example.test/team/bb')
                        """),
                        {
                            "id": uuid4(),
                            "installation_id": other_provider_installation,
                            "workspace_id": workspace_id,
                        },
                    )
                    page_past_non_bitbucket = await store.list_bindings_page(
                        workspace_id=workspace_id, installation_id=None, after=None, limit=2
                    )
                    assert [item.full_name for item in page_past_non_bitbucket] == [
                        "team/aa",
                        "team/b",
                        "team/c",
                    ]
                    for index in range(101):
                        await store.bind_repository(
                            workspace_id=workspace_id,
                            installation_id=installation_id,
                            repository_uuid=uuid4(),
                            full_name=f"batch/{index:03}",
                            default_branch="main",
                            web_url=f"https://bitbucket.org/batch/{index:03}",
                        )
                    page_statements: list[str] = []
                    page_limits: list[int] = []

                    def capture_page(
                        _connection: Connection,
                        _cursor: object,
                        statement: str,
                        parameters: object,
                        _context: object,
                        _executemany: bool,
                    ) -> None:
                        if "FROM repositories" in statement and "LIMIT" in statement.upper():
                            page_statements.append(statement)
                            assert isinstance(parameters, Mapping)
                            page_limits.extend(
                                value for value in parameters.values() if value == 101
                            )

                    event.listen(engine.sync_engine, "before_cursor_execute", capture_page)
                    try:
                        all_scoped = await store.list_bindings(
                            workspace_id=workspace_id, installation_id=installation_id
                        )
                    finally:
                        event.remove(engine.sync_engine, "before_cursor_execute", capture_page)
                    assert len(all_scoped) == 104
                    assert [item.full_name for item in all_scoped[:2]] == ["batch/000", "batch/001"]
                    assert all(item.installation_id == installation_id for item in all_scoped)
                    assert len(page_statements) == 2
                    assert all("LIMIT" in statement.upper() for statement in page_statements)
                    assert page_limits == [101, 101]
                    await session.commit()
                    renamed_id = next(
                        item.repository_id for item in all_scoped if item.full_name == "batch/099"
                    )
                    renamed = False

                    def rename_before_details(
                        _connection: Connection,
                        _cursor: object,
                        statement: str,
                        _parameters: object,
                        _context: object,
                        _executemany: bool,
                    ) -> None:
                        nonlocal renamed
                        if renamed or "FROM bitbucket_repository_identities" not in statement:
                            return
                        renamed = True
                        db.execute(
                            text("UPDATE repositories SET full_name='batch/zzz' WHERE id=:id"),
                            {"id": renamed_id},
                        )
                        db.commit()

                    event.listen(engine.sync_engine, "before_cursor_execute", rename_before_details)
                    try:
                        after_rename = await store.list_bindings(
                            workspace_id=workspace_id, installation_id=installation_id
                        )
                    finally:
                        event.remove(
                            engine.sync_engine, "before_cursor_execute", rename_before_details
                        )
                    assert renamed
                    assert len(after_rename) == len(all_scoped)
                    assert {item.repository_id for item in after_rename} == {
                        item.repository_id for item in all_scoped
                    }
                    renamed_binding = next(
                        item for item in after_rename if item.repository_id == renamed_id
                    )
                    assert renamed_binding.full_name == "batch/zzz"
                    assert renamed_binding.position_full_name == "batch/099"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_large_binding_pages_use_scoped_ordered_indexes(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace_id, foreign_workspace_id = uuid4(), uuid4()
    installation_id, second_installation_id, foreign_installation_id = uuid4(), uuid4(), uuid4()
    for tenant in (workspace_id, foreign_workspace_id):
        db.execute(
            text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
            {"id": tenant, "name": str(tenant)},
        )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
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
                    for tenant, owned_installation in (
                        (workspace_id, installation_id),
                        (workspace_id, second_installation_id),
                        (foreign_workspace_id, foreign_installation_id),
                    ):
                        await store.create_connection(
                            workspace_id=tenant,
                            installation_id=owned_installation,
                            bitbucket_workspace_uuid=uuid4(),
                            token=cipher.encrypt(
                                "token",
                                context=CredentialContext(owned_installation, tenant),
                                purpose=CredentialPurpose.TOKEN,
                            ),
                        )
                    await session.execute(
                        text("""
                            INSERT INTO repositories
                                (id, provider_installation_id, workspace_id, external_id,
                                 full_name, default_branch, web_url)
                            SELECT md5('repository-' || number::text)::uuid,
                                   CASE WHEN number >= 6000 THEN :foreign_installation_id
                                       WHEN number % 2 = 0 THEN :installation_id
                                       ELSE :second_installation_id END,
                                   CASE WHEN number >= 6000 THEN :foreign_workspace_id
                                       ELSE :workspace_id END,
                                   nextval('bitbucket_repository_external_id_seq'),
                                   'large/' || lpad((number % 6000)::text, 4, '0'),
                                   'main', 'https://bitbucket.org/large/' || number::text
                            FROM generate_series(0, 11999) AS number
                        """),
                        {
                            "installation_id": installation_id,
                            "second_installation_id": second_installation_id,
                            "foreign_installation_id": foreign_installation_id,
                            "workspace_id": workspace_id,
                            "foreign_workspace_id": foreign_workspace_id,
                        },
                    )
                    await session.execute(
                        text("""
                            INSERT INTO bitbucket_repository_identities
                                (repository_id, provider_installation_id, workspace_id,
                                 repository_external_id, repository_uuid)
                            SELECT id, provider_installation_id, workspace_id, external_id,
                                   md5('remote-' || full_name)::uuid
                            FROM repositories
                            WHERE provider_installation_id IN
                                (:installation_id, :second_installation_id,
                                 :foreign_installation_id)
                        """),
                        {
                            "installation_id": installation_id,
                            "second_installation_id": second_installation_id,
                            "foreign_installation_id": foreign_installation_id,
                        },
                    )
                    await session.execute(
                        text("""
                            INSERT INTO bitbucket_repository_webhook_secrets (repository_id)
                            SELECT id FROM repositories
                            WHERE provider_installation_id IN
                                (:installation_id, :second_installation_id,
                                 :foreign_installation_id)
                        """),
                        {
                            "installation_id": installation_id,
                            "second_installation_id": second_installation_id,
                            "foreign_installation_id": foreign_installation_id,
                        },
                    )
                    for table_name in (
                        "repositories",
                        "bitbucket_repository_identities",
                        "bitbucket_repository_webhook_secrets",
                    ):
                        await session.execute(text(f"ANALYZE {table_name}"))
                    deep_id = await session.scalar(
                        text("""
                            SELECT id FROM repositories
                            WHERE workspace_id = :workspace_id AND full_name = 'large/2500'
                        """),
                        {"workspace_id": workspace_id},
                    )
                    assert isinstance(deep_id, UUID)
                    captured: list[tuple[str, object]] = []

                    def capture_page(
                        _connection: Connection,
                        _cursor: object,
                        statement: str,
                        parameters: object,
                        _context: object,
                        _executemany: bool,
                    ) -> None:
                        if "FROM repositories" in statement and "LIMIT" in statement.upper():
                            captured.append((statement, parameters))

                    for scope_id, first_name, index_names in (
                        (
                            installation_id,
                            "large/2502",
                            (
                                "ix_repositories_available_installation_name_id",
                                "ix_repositories_available_workspace_name_id",
                            ),
                        ),
                        (None, "large/2501", ("ix_repositories_available_workspace_name_id",)),
                    ):
                        captured.clear()
                        event.listen(engine.sync_engine, "before_cursor_execute", capture_page)
                        try:
                            page = await store.list_bindings_page(
                                workspace_id=workspace_id,
                                installation_id=scope_id,
                                after=BindingPosition(
                                    workspace_id, installation_id, "large/2500", deep_id
                                ),
                                limit=100,
                            )
                        finally:
                            event.remove(engine.sync_engine, "before_cursor_execute", capture_page)
                        assert len(page) == 101
                        assert page[0].full_name == first_name
                        assert all(
                            item.installation_id in (installation_id, second_installation_id)
                            for item in page
                        )
                        assert len(captured) == 1
                        sql, parameters = captured[0]
                        explained = await connection.exec_driver_sql(
                            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                            cast(Any, parameters),
                        )
                        plan = explained.scalar_one()[0]["Plan"]
                        nodes = [plan]
                        for node in nodes:
                            nodes.extend(node.get("Plans", []))
                        assert any(node.get("Index Name") in index_names for node in nodes), plan
                        assert all(node["Node Type"] != "Sort" for node in nodes), plan
                        assert any(
                            "ROW(" in node.get("Index Cond", "")
                            and "full_name" in node["Index Cond"]
                            for node in nodes
                        ), plan
                        assert (
                            sum(node.get("Rows Removed by Filter", 0) for node in nodes) <= 101
                        ), plan
        finally:
            await engine.dispose()

    asyncio.run(scenario())
