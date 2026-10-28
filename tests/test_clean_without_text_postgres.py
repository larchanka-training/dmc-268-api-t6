"""The empty-file proof survives production snapshot storage and schema rollback."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    StoreDiffSnapshot,
    is_proven_empty_snapshot,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import HEAD, PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.vcs_facts import EMPTY_BLOB_SHA as _EMPTY_BLOB


@pytest.fixture
def empty_file_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def store_empty_addition(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory.begin() as session:
            await session.execute(
                text("UPDATE runs SET state='running' WHERE id=:run"), {"run": RUN_ATTEMPTED}
            )
        async with SqlAlchemyRunProcessingUnitOfWork(factory) as uow:
            await StoreDiffSnapshot(uow.repository).execute(
                run_id=RUN_ATTEMPTED,
                code_change_id=PR_NEW,
                head_sha=HEAD,
                files=[
                    DiffSnapshot(
                        "pkg/__init__.py",
                        None,
                        blob_sha=_EMPTY_BLOB,
                        status="added",
                        omission_reason="empty_file",
                    )
                ],
            )
            await uow.commit()
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_empty_file_omission_persists_and_reloads_through_real_repository(
    empty_file_env: Env,
) -> None:
    async def scenario() -> None:
        await store_empty_addition(empty_file_env)
        engine = empty_file_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            snapshots = await SqlAlchemyRunRepository(
                factory, allow_unscoped=True
            ).get_run_snapshots(RUN_ATTEMPTED)
            assert snapshots == [
                DiffSnapshot(
                    "pkg/__init__.py",
                    None,
                    blob_sha=_EMPTY_BLOB,
                    status="added",
                    omission_reason="empty_file",
                )
            ]
            assert is_proven_empty_snapshot(snapshots[0]) is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_downgrade_rewrites_empty_file_to_missing_patch_before_restoring_old_constraint(
    empty_file_env: Env,
) -> None:
    asyncio.run(store_empty_addition(empty_file_env))
    engine = create_engine(empty_file_env.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'SET search_path TO "{empty_file_env.schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            throttle_schema = text(
                "SELECT (SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='code_index_states' "
                "AND column_name IN ('review_full_requested_at', 'review_full_requested_sha')), "
                "(SELECT count(*) FROM pg_constraint "
                "WHERE conname='ck_code_index_states_review_full_sha_at' "
                "AND connamespace=current_schema()::regnamespace), "
                "(SELECT count(*) FROM pg_constraint "
                "WHERE conname='ck_code_index_states_wake_live' "
                "AND connamespace=current_schema()::regnamespace)"
            )
            assert tuple(connection.execute(throttle_schema).one()) == (2, 1, 1)
            manifest_schema = text(
                "SELECT (SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name='context_manifests'), "
                "(SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='context_manifests' "
                "AND column_name IN ('policy_snapshot_id', 'rendered_messages_sha256', "
                "'prompt_checksum', 'model_profile_version') AND is_nullable='NO')"
            )
            assert tuple(connection.execute(manifest_schema).one()) == (1, 4)
            gitlab_schema = text(
                "SELECT (SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=current_schema() "
                "AND table_name IN ('gitlab_connections', 'gitlab_project_bindings')), "
                "(SELECT count(*) FROM pg_constraint "
                "WHERE connamespace=current_schema()::regnamespace "
                "AND conname IN ('uq_provider_installations_owner', 'uq_repositories_owner', "
                "'ck_gitlab_connections_generation', 'ck_gitlab_connections_provider', "
                "'ck_gitlab_project_bindings_project_positive')), "
                "(SELECT count(*) FROM pg_constraint "
                "WHERE connamespace=current_schema()::regnamespace AND contype='f' "
                "AND conrelid IN ('gitlab_connections'::regclass, "
                "'gitlab_project_bindings'::regclass))"
            )
            assert tuple(connection.execute(gitlab_schema).one()) == (2, 5, 3)
            connection.execute(
                text(
                    "INSERT INTO code_index_states "
                    "(repository_id, review_full_requested_at, review_full_requested_sha) "
                    "VALUES (:repository, now(), :sha)"
                ),
                {"repository": REPO_A, "sha": HEAD},
            )
            throttle_row = text(
                "SELECT review_full_requested_at, review_full_requested_sha "
                "FROM code_index_states WHERE repository_id=:repository"
            )
            throttle_before = connection.execute(throttle_row, {"repository": REPO_A}).one()
            connection.commit()
            command.downgrade(config, "20261026_0042")
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261026_0042"
            )
            assert tuple(connection.execute(throttle_schema).one()) == (2, 1, 1)
            assert connection.execute(throttle_row, {"repository": REPO_A}).one() == throttle_before
            assert tuple(connection.execute(manifest_schema).one()) == (1, 4)
            assert tuple(connection.execute(gitlab_schema).one()) == (2, 5, 3)

            assert (
                connection.scalar(
                    text(
                        "SELECT omission_reason FROM code_change_diffs "
                        "WHERE filename='pkg/__init__.py'"
                    )
                )
                == "missing_patch"
            )
            with (
                pytest.raises(IntegrityError, match="ck_code_change_diffs_omission_reason"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        "UPDATE code_change_diffs SET omission_reason='empty_file' "
                        "WHERE filename='pkg/__init__.py'"
                    )
                )
            connection.commit()
            command.upgrade(config, "head")
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261027_0051"
            )
            assert tuple(connection.execute(throttle_schema).one()) == (2, 1, 1)
            assert connection.execute(throttle_row, {"repository": REPO_A}).one() == throttle_before
            assert tuple(connection.execute(manifest_schema).one()) == (1, 4)
            assert tuple(connection.execute(gitlab_schema).one()) == (2, 5, 3)
            assert (
                connection.scalar(
                    text(
                        "SELECT omission_reason FROM code_change_diffs "
                        "WHERE filename='pkg/__init__.py'"
                    )
                )
                == "missing_patch"
            )
    finally:
        engine.dispose()
