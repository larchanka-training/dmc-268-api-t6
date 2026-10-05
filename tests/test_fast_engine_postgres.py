"""deep is phase 3: repositories and queued runs move to fast, nothing goes to review.run.deep.

Opt-in with ``TEST_DATABASE_URL`` (#52).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)

LONG_AGO = datetime(2026, 9, 1, tzinfo=UTC)


def _seed(connection: Connection, engine: str) -> dict[str, UUID]:
    ids = {name: uuid4() for name in ("ws", "inst", "repo", "rule", "prompt")}

    def run(statement: str, **values: Any) -> None:
        connection.execute(text(statement), values)

    run("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'W', 0)", id=ids["ws"])
    run(
        "INSERT INTO provider_installations (id, workspace_id, provider, external_id, metadata) "
        "VALUES (:id, :ws, 'github', 17, '{}'::jsonb)",
        id=ids["inst"],
        ws=ids["ws"],
    )
    run(
        "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
        "default_branch, web_url, default_engine) VALUES (:id, :inst, 101, 'octo/repo', "
        "'main', 'https://github.test/octo/repo', CAST(:engine AS engine))",
        id=ids["repo"],
        inst=ids["inst"],
        engine=engine,
    )
    run(
        "INSERT INTO rule_versions (id, repository_id, version, rules, checksum, is_active) "
        "VALUES (:id, :repo, 1, '[]'::jsonb, :checksum, true)",
        id=ids["rule"],
        repo=ids["repo"],
        checksum="a" * 64,
    )
    run(
        "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
        "VALUES (:id, 'review.system', 1, 'system', :checksum, true)",
        id=ids["prompt"],
        checksum="c" * 64,
    )
    return ids


def _add_run(connection: Connection, ids: dict[str, UUID], number: int, state: str) -> UUID:
    pr, run_id = uuid4(), uuid4()
    connection.execute(
        text(
            "INSERT INTO code_changes (id, repository_id, external_id, external_number, title, "
            "source_branch, target_branch, base_sha, head_sha, state, web_url) VALUES (:id, "
            ":repo, :n, :n, 'PR', 'f', 'main', :base, :head, 'open', 'https://github.test/pr')"
        ),
        {"id": pr, "repo": ids["repo"], "n": number, "base": "b" * 40, "head": "e" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, state, trigger, "
            "idempotency_key, engine, rule_version_id, prompt_version_id, available_at, "
            "created_at) VALUES (:id, :pr, :base, 'main', :head, CAST(:state AS run_state), "
            "'webhook', :key, 'deep', :rule, :prompt, :at, :at)"
        ),
        {
            "id": run_id,
            "pr": pr,
            "base": "b" * 40,
            "head": "e" * 40,
            "state": state,
            "key": uuid4().hex + uuid4().hex,
            "rule": ids["rule"],
            "prompt": ids["prompt"],
            "at": LONG_AGO,
        },
    )
    return run_id


@pytest.fixture
def schema() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    name = f"test_fast_engine_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(name))
            connection.execute(text(f'SET search_path TO "{name}"'))
            connection.commit()
            yield database_url, name
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(name, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


def _migrate(database_url: str, name: str, revision: str) -> Connection:
    engine = create_engine(database_url)
    connection = engine.connect()
    connection.execute(text(f'SET search_path TO "{name}"'))
    connection.commit()
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, revision)
    return connection


@pytest.mark.integration
def test_migration_moves_repositories_and_queued_runs_to_fast(schema: tuple[str, str]) -> None:
    database_url, name = schema
    connection = _migrate(database_url, name, "20261005_0024")
    ids = _seed(connection, "deep")
    queued = _add_run(connection, ids, 1, "queued")
    finished = _add_run(connection, ids, 2, "succeeded")
    connection.commit()
    command.upgrade(_config(connection), "head")
    engines = {
        row[0]: row[1] for row in connection.execute(text("SELECT id, engine::text FROM runs"))
    }
    repository = connection.scalar(text("SELECT default_engine::text FROM repositories"))
    connection.close()

    assert repository == "fast"
    assert engines == {queued: "fast", finished: "deep"}


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


class Publisher:
    def __init__(self) -> None:
        self.runs: list[tuple[UUID, str]] = []

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        self.runs.append((message.run_id, message.engine))

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        return None


@pytest.mark.integration
def test_reconciler_does_not_republish_into_the_deep_queue(schema: tuple[str, str]) -> None:
    database_url, name = schema
    connection = _migrate(database_url, name, "head")
    ids = _seed(connection, "fast")
    # A legacy deep run written after the migration, e.g. by an old API process.
    deep = _add_run(connection, ids, 1, "queued")
    connection.commit()
    connection.close()

    async def reconcile() -> list[tuple[UUID, str]]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={name}"}, poolclass=NullPool
        )
        publisher = Publisher()
        await ReconcileRuns(
            uow_factory=partial(SqlAlchemyRunLifecycleUnitOfWork, async_sessionmaker(engine)),
            run_publisher=publisher,
            review_queue=publisher,
            now=lambda: LONG_AGO + timedelta(days=1),
        ).execute()
        await engine.dispose()
        return publisher.runs

    published = asyncio.run(reconcile())

    assert deep not in [run_id for run_id, _ in published]
    assert published == []
