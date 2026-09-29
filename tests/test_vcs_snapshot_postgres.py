"""Opt-in PostgreSQL proof for SHA-qualified file lookup across two PRs."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.main import app, get_file_blob_cache, get_run_repository
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    review_files_from_snapshots,
)
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.models import CachedFileBlob, Run
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_test_client import authenticated_test_client


@pytest.fixture
def vcs_database() -> Iterator[tuple[str, str, UUID, UUID, UUID, str, UUID, UUID]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_vcs_snapshot_{uuid4().hex}"
    repository_id, first_pr, second_pr, first_run, second_run = (uuid4() for _ in range(5))
    duplicate_run, fresh_run = uuid4(), uuid4()
    workspace_id, installation_id, prompt_id, rule_id = (uuid4() for _ in range(4))
    blob_sha = "a" * 40
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "20260928_0015")
            connection.execute(
                text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'VCS', 0)"),
                {"id": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :workspace, 'github', 17, '{}'::jsonb)"
                ),
                {"id": installation_id, "workspace": workspace_id},
            )
            connection.execute(
                text(
                    "INSERT INTO repositories "
                    "(id, provider_installation_id, external_id, full_name, "
                    "default_branch, web_url) "
                    "VALUES (:id, :installation, 101, 'octo/repo', 'main', 'https://github.test/octo/repo')"
                ),
                {"id": repository_id, "installation": installation_id},
            )
            connection.execute(
                text(
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                    "VALUES (:id, 'review.system', 1, 'system', :checksum, true)"
                ),
                {"id": prompt_id, "checksum": "c" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO rule_versions "
                    "(id, repository_id, version, rules, checksum, is_active) "
                    "VALUES (:id, :repository, 1, '[]'::jsonb, :checksum, true)"
                ),
                {"id": rule_id, "repository": repository_id, "checksum": "d" * 64},
            )
            now = datetime(2026, 9, 28, tzinfo=UTC)
            for number, pr_id, run_id, path in (
                (7, first_pr, first_run, "src/first.py"),
                (8, second_pr, second_run, "src/renamed.py"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id, repository_id, external_id, external_number, title, source_branch, "
                        "target_branch, base_sha, head_sha, state, web_url) "
                        "VALUES (:id, :repository, :external, :number, 'PR', 'feature', 'main', "
                        ":base, :head, 'open', :url)"
                    ),
                    {
                        "id": pr_id,
                        "repository": repository_id,
                        "external": 900 + number,
                        "number": number,
                        "base": "b" * 40,
                        "head": "e" * 40,
                        "url": f"https://github.test/octo/repo/pull/{number}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO runs "
                        "(id, code_change_id, base_sha, head_sha, state, trigger, idempotency_key, "
                        "engine, rule_version_id, prompt_version_id, available_at) "
                        "VALUES (:id, :pr, :base, :head, 'succeeded', 'manual', :key, "
                        "'fast', :rule, :prompt, :available)"
                    ),
                    {
                        "id": run_id,
                        "pr": pr_id,
                        "base": "b" * 40,
                        "head": "e" * 40,
                        "key": str(number) * 64,
                        "rule": rule_id,
                        "prompt": prompt_id,
                        "available": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO code_change_diffs "
                        "(id, code_change_id, head_sha, filename, patch) "
                        "VALUES (:id, :pr, :head, :path, :patch)"
                    ),
                    {
                        "id": uuid4(),
                        "pr": pr_id,
                        "head": "e" * 40,
                        "path": path,
                        "patch": f"diff --git a/{path} b/{path}\n@@ -1 +1 @@\n-old\n+new",
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO code_change_diffs "
                    "(id, code_change_id, head_sha, filename, patch) "
                    "VALUES (:id, :pr, :head, 'src/pre_migration.py', NULL)"
                ),
                {"id": uuid4(), "pr": first_pr, "head": "e" * 40},
            )
            for path, patch in (
                ("package-lock.json", "@@ -1 +1 @@\n-old\n+generated"),
                ("dist/app.min.js", "@@ -1 +1 @@\n-old\n+generated"),
                (
                    "assets/logo.png",
                    "diff --git a/assets/logo.png b/assets/logo.png\n"
                    "Binary files a/assets/logo.png and b/assets/logo.png differ",
                ),
            ):
                connection.execute(
                    text(
                        "INSERT INTO code_change_diffs "
                        "(id, code_change_id, head_sha, filename, patch) "
                        "VALUES (:id, :pr, :head, :path, :patch)"
                    ),
                    {
                        "id": uuid4(),
                        "pr": first_pr,
                        "head": "e" * 40,
                        "path": path,
                        "patch": patch,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO runs "
                    "(id, code_change_id, base_sha, head_sha, state, trigger, idempotency_key, "
                    "engine, rule_version_id, prompt_version_id, available_at) "
                    "VALUES (:id, :pr, :base, :head, 'succeeded', 'manual', :key, "
                    "'fast', :rule, :prompt, :available)"
                ),
                {
                    "id": duplicate_run,
                    "pr": first_pr,
                    "base": "b" * 40,
                    "head": "e" * 40,
                    "key": "9" * 64,
                    "rule": rule_id,
                    "prompt": prompt_id,
                    "available": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO cached_file_blobs "
                    "(id, code_change_id, head_sha, path, content, expires_at) "
                    "VALUES (:id, :pr, :head, 'src/first.py', 'legacy-path-ref', :expires)"
                ),
                {
                    "id": uuid4(),
                    "pr": first_pr,
                    "head": "e" * 40,
                    "expires": now + timedelta(days=7),
                },
            )
            connection.commit()
            command.upgrade(config, "head")
            columns = {
                column["name"] for column in inspect(connection).get_columns("code_change_diffs")
            }
            assert {
                "run_id",
                "blob_sha",
                "status",
                "previous_filename",
                "omission_reason",
                "summary_only",
            } <= columns
            copied = connection.execute(
                text("SELECT run_id, filename, patch FROM code_change_diffs")
            ).all()
            assert {row[0] for row in copied} == {first_run, second_run, duplicate_run}
            assert len(copied) == 11
            legacy_omissions = connection.execute(
                text(
                    "SELECT filename, omission_reason FROM code_change_diffs "
                    "WHERE run_id = :run AND filename IN "
                    "('package-lock.json', 'dist/app.min.js', 'assets/logo.png')"
                ),
                {"run": duplicate_run},
            ).all()
            assert {row[0]: row[1] for row in legacy_omissions} == {
                "package-lock.json": "generated",
                "dist/app.min.js": "generated",
                "assets/logo.png": "binary",
            }
            assert connection.scalar(text("SELECT count(*) FROM cached_file_blobs")) == 0
            assert (
                connection.scalar(
                    text("SELECT diff_snapshotted_at FROM runs WHERE id = :id"),
                    {"id": duplicate_run},
                )
                is not None
            )
            connection.execute(
                text(
                    "UPDATE code_change_diffs SET blob_sha = :blob, status = 'renamed' "
                    "WHERE run_id IN (:first, :second) AND filename <> 'src/pre_migration.py'"
                ),
                {"blob": blob_sha, "first": first_run, "second": second_run},
            )
            connection.execute(
                text(
                    "INSERT INTO runs "
                    "(id, code_change_id, base_sha, head_sha, state, trigger, idempotency_key, "
                    "engine, rule_version_id, prompt_version_id, available_at) "
                    "VALUES (:id, :pr, :base, :head, 'succeeded', 'manual', :key, "
                    "'fast', :rule, :prompt, :available)"
                ),
                {
                    "id": fresh_run,
                    "pr": first_pr,
                    "base": "b" * 40,
                    "head": "e" * 40,
                    "key": "0" * 64,
                    "rule": rule_id,
                    "prompt": prompt_id,
                    "available": now,
                },
            )
            connection.commit()
            yield (
                database_url,
                schema,
                first_run,
                second_run,
                repository_id,
                blob_sha,
                duplicate_run,
                fresh_run,
            )
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_file_endpoint_resolves_two_pr_paths_through_one_immutable_blob(
    vcs_database: tuple[str, str, UUID, UUID, UUID, str, UUID, UUID],
) -> None:
    (
        database_url,
        schema,
        first_run,
        second_run,
        repository_id,
        blob_sha,
        duplicate_run,
        fresh_run,
    ) = vcs_database
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 9, 28, tzinfo=UTC)]
    cache = SqlAlchemyBlobCache(factory, now=lambda: clock[0])
    try:
        asyncio.run(
            cache.put(BlobCacheKey(repository_id, blob_sha), "same\nblob", ttl=timedelta(days=7))
        )
        app.dependency_overrides[get_run_repository] = lambda: SqlAlchemyRunRepository(factory)
        app.dependency_overrides[get_file_blob_cache] = lambda: cache
        client = authenticated_test_client(app)
        first = client.get(f"/api/runs/{first_run}/files", params={"path": "src/first.py"})
        second = client.get(f"/api/runs/{second_run}/files", params={"path": "src/renamed.py"})
        missing = client.get(f"/api/runs/{first_run}/files", params={"path": "src/renamed.py"})
        legacy = client.get(f"/api/runs/{first_run}/files", params={"path": "src/pre_migration.py"})
        inherited = client.get(f"/api/runs/{fresh_run}/diff")
        inherited_blob = client.get(f"/api/runs/{fresh_run}/files", params={"path": "src/first.py"})
        copied_legacy = client.get(f"/api/runs/{duplicate_run}/diff")

        async def count_blobs() -> int:
            async with factory() as session:
                return int(
                    (await session.scalar(select(func.count()).select_from(CachedFileBlob))) or 0
                )

        assert asyncio.run(count_blobs()) == 1
        clock[0] += timedelta(days=7)
        expired = client.get(f"/api/runs/{second_run}/files", params={"path": "src/renamed.py"})
        assert first.status_code == second.status_code == 200
        assert first.json()["lines"] == second.json()["lines"] == ["same", "blob"]
        assert missing.status_code == 404
        assert legacy.status_code == 404
        assert inherited.status_code == 200 and inherited.json() == []
        assert inherited_blob.status_code == 404
        assert copied_legacy.status_code == 200 and len(copied_legacy.json()) == 5

        async def legacy_review_files() -> tuple[tuple[str, ...], tuple[str, ...]]:
            snapshots = await SqlAlchemyRunRepository(factory).get_run_snapshots(duplicate_run)
            assert snapshots is not None
            changed, omitted = review_files_from_snapshots(snapshots)
            return tuple(file.path for file in changed), omitted

        changed_paths, omitted_paths = asyncio.run(legacy_review_files())
        assert changed_paths == ("src/first.py",)
        assert omitted_paths == (
            "assets/logo.png",
            "dist/app.min.js",
            "package-lock.json",
            "src/pre_migration.py",
        )
        repository = SqlAlchemyRunRepository(factory)
        first_snapshot = DiffSnapshot(filename="src/new.py", patch="@@ -0,0 +1 @@\n+first")
        retry_snapshot = DiffSnapshot(filename="src/new.py", patch="@@ -0,0 +1 @@\n+retry")

        async def store_fresh_run() -> tuple[list[DiffSnapshot], list[DiffSnapshot]]:
            async with factory() as session:
                code_change_id = await session.scalar(
                    select(Run.code_change_id).where(Run.id == fresh_run)
                )
            assert code_change_id is not None
            stored = await repository.store_diff_snapshots(
                fresh_run, code_change_id, "e" * 40, [first_snapshot]
            )
            repeated = await repository.store_diff_snapshots(
                fresh_run, code_change_id, "e" * 40, [retry_snapshot]
            )
            return stored, repeated

        stored, repeated = asyncio.run(store_fresh_run())
        assert stored == [first_snapshot]
        assert repeated == [first_snapshot]
        assert client.get(f"/api/runs/{fresh_run}/diff").json() == [
            {"filename": "src/new.py", "patch": first_snapshot.patch}
        ]
        assert "src/first.py" in {
            item["filename"] for item in client.get(f"/api/runs/{first_run}/diff").json()
        }
        assert expired.status_code == 410
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
