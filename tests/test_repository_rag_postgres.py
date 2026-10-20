"""The repository RAG toggle on PostgreSQL: off by default, onboarding never turns it on, and the
settings API serves it with the index status (#145); turning it off purges the index (#143)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.auth.application.scope import AuthScope
from app.modules.code_index.domain.enums import IndexStatus, PurgeReason
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from app.modules.repositories.infrastructure.repository_settings import (
    SqlAlchemyRepositorySettingsUnitOfWork,
)
from tests.code_index_postgres import GRAPH_ROWS, collect_garbage, graph_rows, publish_index
from tests.portal_postgres import INSTALL_A, REPO_A, REPO_B, WS_A, Env, api, portal_schema, scalar
from tests.test_code_index_store_postgres import _Index, _state

_RAG_COLUMN = (
    "SELECT is_nullable, column_default FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'repositories' "
    "AND column_name = 'rag_enabled'"
)
SHA_A = "a" * 40
SHA_B = "b" * 40
DISABLED = {
    "status": "disabled",
    "indexedCommitSha": None,
    "indexedAt": None,
    "fileCount": 0,
    "symbolCount": 0,
    "lastErrorCode": None,
}
# A published index of octo/a: 12 files, 340 symbols at SHA_A.
_PUBLISHED = {
    "status": "ready",
    "indexed_commit_sha": SHA_A,
    "target_commit_sha": None,
    "published_at": "2026-10-16 11:30:00+02",
    "file_count": 12,
    "symbol_count": 340,
    "error_code": None,
    "last_error_code": None,
}
_INSERT_STATE = (
    "INSERT INTO code_index_states (repository_id, status, indexed_commit_sha, "
    "target_commit_sha, published_at, file_count, symbol_count, error_code, last_error_code) "
    "VALUES (:repository, CAST(:status AS code_index_status), :indexed_commit_sha, "
    ":target_commit_sha, CAST(:published_at AS timestamptz), :file_count, :symbol_count, "
    ":error_code, :last_error_code)"
)


@pytest.fixture
def rag_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_repository_rag_{uuid4().hex}"
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


@pytest.fixture
def rag_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _sync(factory: async_sessionmaker[AsyncSession]) -> SyncInstallationRepositories:
    return SyncInstallationRepositories(
        uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
        rule_sets=load_default_rule_sets(Path("review/rules")),
    )


def _execute(factory: async_sessionmaker[AsyncSession], sql: str, **values: Any) -> None:
    async def write() -> None:
        async with factory.begin() as session:
            await session.execute(text(sql), values)

    asyncio.run(write())


async def _onboard(sync: SyncInstallationRepositories, external_id: int, name: str) -> UUID:
    (result,) = await sync.execute(
        provider_installation_id=INSTALL_A,
        repositories=(
            RepositoryOnboardingInput(
                RepositorySnapshot(external_id, name, "main", f"https://github.test/{name}"),
                {"Python": 100},
            ),
        ),
    )
    return result.rule_version.repository_id


@pytest.mark.integration
def test_existing_repository_reads_rag_disabled_after_the_upgrade(
    rag_schema: Connection,
) -> None:
    connection = rag_schema
    config = _config(connection)
    command.upgrade(config, "20261015_0032")
    workspace, installation, repository = str(uuid4()), str(uuid4()), str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'legacy', 0)"),
        {"id": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations (id, workspace_id, provider, external_id, "
            "metadata) VALUES (:id, :workspace, 'github', 17, '{}'::jsonb)"
        ),
        {"id": installation, "workspace": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
            "default_branch, web_url) VALUES (:id, :installation, 101, 'octo/legacy', 'main', "
            "'https://github.test/octo/legacy')"
        ),
        {"id": repository, "installation": installation},
    )
    connection.commit()

    command.upgrade(config, "20261016_0033")

    assert connection.execute(text(_RAG_COLUMN)).one() == ("NO", "false")
    assert (
        connection.scalar(
            text("SELECT rag_enabled FROM repositories WHERE id = :id"), {"id": repository}
        )
        is False
    )
    connection.commit()
    command.downgrade(config, "20261015_0032")
    assert connection.execute(text(_RAG_COLUMN)).all() == []


@pytest.mark.integration
def test_onboarding_a_new_repository_leaves_rag_disabled(rag_env: Env) -> None:
    async def exercise() -> object:
        engine = rag_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            repository = await _onboard(_sync(factory), 103, "octo/new")
            async with factory() as session:
                return await session.scalar(
                    text("SELECT rag_enabled FROM repositories WHERE id = :id"),
                    {"id": repository},
                )
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) is False


@pytest.mark.integration
def test_onboarding_an_existing_repository_again_keeps_rag_enabled(rag_env: Env) -> None:
    async def exercise() -> tuple[UUID, object]:
        engine = rag_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET rag_enabled = true WHERE id = :id"),
                    {"id": REPO_A},
                )
            repository = await _onboard(_sync(factory), 101, "octo/a")
            async with factory() as session:
                enabled = await session.scalar(
                    text("SELECT rag_enabled FROM repositories WHERE id = :id"),
                    {"id": REPO_A},
                )
            return repository, enabled
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) == (REPO_A, True)


@pytest.mark.integration
def test_settings_api_reads_rag_disabled_by_default(rag_env: Env) -> None:
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, _):
        detail = client.get(f"/api/repos/{REPO_A}").json()
        listed = client.get("/api/repos").json()

    assert (detail["ragEnabled"], detail["ragIndex"]) == (False, DISABLED)
    assert listed == [detail]


@pytest.mark.integration
def test_settings_api_patch_stores_the_rag_toggle(rag_env: Env) -> None:
    url = f"/api/repos/{REPO_A}"
    stored = "SELECT rag_enabled FROM repositories WHERE id = :id"
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        enabled = client.patch(url, json={"ragEnabled": True})
        stored_enabled = tuple(scalar(factory, stored, id=REPO_A))
        detail = client.get(url).json()
        disabled = client.patch(url, json={"ragEnabled": False})
        stored_disabled = tuple(scalar(factory, stored, id=REPO_A))

    assert enabled.status_code == 200
    # Enabling creates no state row: the index stays empty until a build (#143).
    assert (enabled.json()["ragEnabled"], enabled.json()["ragIndex"]) == (
        True,
        {**DISABLED, "status": "empty"},
    )
    assert stored_enabled == (True,)
    assert detail == enabled.json()
    assert disabled.status_code == 200
    assert (disabled.json()["ragEnabled"], disabled.json()["ragIndex"]) == (False, DISABLED)
    assert stored_disabled == (False,)


@pytest.mark.integration
def test_settings_api_hides_another_workspace_repository_from_the_rag_toggle(
    rag_env: Env,
) -> None:
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        patched = client.patch(f"/api/repos/{REPO_B}", json={"ragEnabled": True})
        read = client.get(f"/api/repos/{REPO_B}")
        stored = tuple(
            scalar(
                factory, "SELECT rag_enabled, enabled FROM repositories WHERE id = :id", id=REPO_B
            )
        )

    assert (patched.status_code, read.status_code) == (404, 404)
    assert stored == (False, True)


@pytest.mark.integration
def test_unavailable_repository_reads_rag_disabled_but_keeps_the_preference(
    rag_env: Env,
) -> None:
    url = f"/api/repos/{REPO_A}"
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        _execute(factory, _INSERT_STATE, repository=REPO_A, **_PUBLISHED)
        _execute(
            factory,
            "UPDATE repositories SET rag_enabled = true, installation_available = false "
            "WHERE id = :id",
            id=REPO_A,
        )
        detail = client.get(url).json()
        listed = client.get("/api/repos").json()
        patched = client.patch(url, json={"ragEnabled": True})
        stored = tuple(
            scalar(factory, "SELECT rag_enabled FROM repositories WHERE id = :id", id=REPO_A)
        )

    assert (detail["ragEnabled"], detail["ragIndex"]) == (False, DISABLED)
    assert listed == [detail]
    assert patched.status_code == 200
    assert (patched.json()["ragEnabled"], patched.json()["ragIndex"]) == (False, DISABLED)
    assert stored == (True,)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("rag_enabled", "state", "expected"),
    [
        pytest.param(True, None, {**DISABLED, "status": "empty"}, id="no-row"),
        pytest.param(
            True,
            {
                **_PUBLISHED,
                "status": "building",
                "indexed_commit_sha": None,
                "published_at": None,
                "file_count": 0,
                "symbol_count": 0,
            },
            {**DISABLED, "status": "building"},
            id="building",
        ),
        pytest.param(
            True,
            {
                **_PUBLISHED,
                "status": "empty",
                "indexed_commit_sha": None,
                "target_commit_sha": SHA_B,
                "published_at": None,
                "file_count": 0,
                "symbol_count": 0,
                "last_error_code": "github_rate_limited",
            },
            {**DISABLED, "status": "empty", "lastErrorCode": "github_rate_limited"},
            id="empty-with-last-error",
        ),
        pytest.param(
            True,
            _PUBLISHED,
            {
                "status": "ready",
                "indexedCommitSha": SHA_A,
                "indexedAt": "2026-10-16T09:30:00Z",
                "fileCount": 12,
                "symbolCount": 340,
                "lastErrorCode": None,
            },
            id="ready-without-target",
        ),
        pytest.param(
            True,
            {**_PUBLISHED, "target_commit_sha": SHA_B},
            {
                "status": "stale",
                "indexedCommitSha": SHA_A,
                "indexedAt": "2026-10-16T09:30:00Z",
                "fileCount": 12,
                "symbolCount": 340,
                "lastErrorCode": None,
            },
            id="stale",
        ),
        pytest.param(
            True,
            {**_PUBLISHED, "target_commit_sha": SHA_B, "last_error_code": "github_rate_limited"},
            {
                "status": "stale",
                "indexedCommitSha": SHA_A,
                "indexedAt": "2026-10-16T09:30:00Z",
                "fileCount": 12,
                "symbolCount": 340,
                "lastErrorCode": "github_rate_limited",
            },
            id="stale-with-last-error",
        ),
        pytest.param(
            True,
            {
                **_PUBLISHED,
                "status": "failed",
                "error_code": "limit_exceeded:symbols",
                "last_error_code": "github_rate_limited",
            },
            {**DISABLED, "status": "failed", "lastErrorCode": "limit_exceeded:symbols"},
            id="failed",
        ),
        pytest.param(
            True,
            {**_PUBLISHED, "last_error_code": "github_rate_limited"},
            {
                "status": "ready",
                "indexedCommitSha": SHA_A,
                "indexedAt": "2026-10-16T09:30:00Z",
                "fileCount": 12,
                "symbolCount": 340,
                "lastErrorCode": "github_rate_limited",
            },
            id="ready-with-soft-code",
        ),
        pytest.param(False, _PUBLISHED, DISABLED, id="toggle-off-while-ready"),
    ],
)
def test_settings_api_derives_the_rag_index_from_the_state_row(
    rag_env: Env,
    rag_enabled: bool,
    state: dict[str, object] | None,
    expected: dict[str, object],
) -> None:
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        _execute(
            factory,
            "UPDATE repositories SET rag_enabled = :enabled WHERE id = :id",
            enabled=rag_enabled,
            id=REPO_A,
        )
        if state is not None:
            _execute(factory, _INSERT_STATE, repository=REPO_A, **state)
        detail = client.get(f"/api/repos/{REPO_A}").json()
        listed = client.get("/api/repos").json()

    assert (detail["ragEnabled"], detail["ragIndex"]) == (rag_enabled, expected)
    assert listed == [detail]


# --- turning the toggle off purges the index (#143) ----------------------------------------


@pytest.mark.integration
def test_settings_api_patch_rag_off_purges_the_index_and_gc_deletes_its_rows(
    rag_env: Env,
) -> None:
    url = f"/api/repos/{REPO_A}"
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        _execute(factory, "UPDATE repositories SET rag_enabled = true WHERE id = :id", id=REPO_A)
        index = _Index(factory, REPO_A, REPO_B)
        asyncio.run(publish_index(index, REPO_A))
        published = asyncio.run(graph_rows(index, REPO_A))
        disabled = client.patch(url, json={"ragEnabled": False})
        detail = client.get(url).json()
        purged = asyncio.run(_state(index, REPO_A))
        kept = asyncio.run(graph_rows(index, REPO_A))
        asyncio.run(collect_garbage(index))
        collected = asyncio.run(graph_rows(index, REPO_A))
        before_repeat = asyncio.run(_state(index, REPO_A))
        repeated = client.patch(url, json={"ragEnabled": False})
        after_repeat = asyncio.run(_state(index, REPO_A))

    assert published == GRAPH_ROWS
    assert disabled.status_code == 200
    # The response is the read after the purge, the same as a later GET.
    assert (disabled.json()["ragEnabled"], disabled.json()["ragIndex"]) == (False, DISABLED)
    assert disabled.json() == detail
    assert purged is not None
    assert (purged.generation, purged.status, purged.purge_reason) == (
        2,
        IndexStatus.EMPTY,
        PurgeReason.DISABLED,
    )
    # The purge is logical in the request; the GC leader task deletes the rows.
    assert kept == GRAPH_ROWS
    assert collected == (0, 0, 0, 0)
    # A repeated PATCH changes nothing.
    assert repeated.status_code == 200
    assert repeated.json() == detail
    assert before_repeat is not None and before_repeat.generation == 2
    assert after_repeat == before_repeat


@pytest.mark.integration
def test_settings_api_patch_rag_off_purges_an_unavailable_repository_too(rag_env: Env) -> None:
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        index = _Index(factory, REPO_A, REPO_B)
        asyncio.run(publish_index(index, REPO_A))
        _execute(
            factory,
            "UPDATE repositories SET rag_enabled = true, installation_available = false "
            "WHERE id = :id",
            id=REPO_A,
        )
        patched = client.patch(f"/api/repos/{REPO_A}", json={"ragEnabled": False})
        state = asyncio.run(_state(index, REPO_A))

    assert patched.status_code == 200
    assert state is not None
    assert (state.generation, state.status, state.purge_reason) == (
        2,
        IndexStatus.EMPTY,
        PurgeReason.DISABLED,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "body",
    [{"enabled": False}, {"branchFilter": ["main"]}],
    ids=["enabled", "branch-filter"],
)
def test_settings_api_patch_without_rag_enabled_leaves_the_index_alone(
    rag_env: Env, body: dict[str, object]
) -> None:
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        _execute(factory, "UPDATE repositories SET rag_enabled = true WHERE id = :id", id=REPO_A)
        index = _Index(factory, REPO_A, REPO_B)
        asyncio.run(publish_index(index, REPO_A))
        before = asyncio.run(_state(index, REPO_A))
        patched = client.patch(f"/api/repos/{REPO_A}", json=body)
        after = asyncio.run(_state(index, REPO_A))
        rows = asyncio.run(graph_rows(index, REPO_A))

    assert patched.status_code == 200
    assert before is not None and before.status is IndexStatus.READY
    assert after == before
    assert rows == GRAPH_ROWS


@pytest.mark.integration
def test_settings_api_patch_rag_off_purges_nothing_in_another_workspace(rag_env: Env) -> None:
    with api(rag_env, AuthScope(42, (WS_A,))) as (client, factory):
        index = _Index(factory, REPO_A, REPO_B)
        asyncio.run(publish_index(index, REPO_B))
        before = asyncio.run(_state(index, REPO_B))
        patched = client.patch(f"/api/repos/{REPO_B}", json={"ragEnabled": False})
        after = asyncio.run(_state(index, REPO_B))

    assert patched.status_code == 404
    assert before is not None and before.generation == 1
    assert after == before


@pytest.mark.integration
def test_the_scoped_purger_leaves_a_repository_outside_the_scope_alone(rag_env: Env) -> None:
    scope = AuthScope(42, (WS_A,))
    with api(rag_env, scope) as (_, factory):
        index = _Index(factory, REPO_A, REPO_B)
        asyncio.run(publish_index(index, REPO_A, REPO_B))
        before = asyncio.run(_state(index, REPO_B))

        async def purge_both() -> None:
            async with SqlAlchemyRepositorySettingsUnitOfWork(factory, scope) as uow:
                for repository_id in (REPO_A, REPO_B):
                    await uow.index_purger.purge(repository_id, PurgeReason.DISABLED)
                await uow.commit()

        asyncio.run(purge_both())
        visible = asyncio.run(_state(index, REPO_A))
        hidden = asyncio.run(_state(index, REPO_B))

    assert visible is not None
    assert (visible.generation, visible.status, visible.purge_reason) == (
        2,
        IndexStatus.EMPTY,
        PurgeReason.DISABLED,
    )
    assert before is not None and before.status is IndexStatus.READY
    assert hidden == before
