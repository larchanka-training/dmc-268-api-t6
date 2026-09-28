"""A historical reviewer request must never become an ai-review label opt-in."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestState,
)
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.models import CodeChange

_NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
_BASE = "b" * 40
_HEAD = "a" * 40


@pytest.fixture
def pre_label_database() -> Iterator[tuple[Connection, str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_label_intent_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection, database_url, schema
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_upgrade_preserves_old_pr_and_run_without_granting_label_intent(
    pre_label_database: tuple[Connection, str, str],
) -> None:
    connection, database_url, schema = pre_label_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20260928_0018")
    workspace_id, installation_id, repository_id, pr_id, run_id = (uuid4() for _ in range(5))
    prompt_id, rule_id = uuid4(), uuid4()
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'label test', 0)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace_id, 'github', 17, '{}'::jsonb)"
        ),
        {"id": installation_id, "workspace_id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation_id, 101, 'octo/repo', 'main', "
            "'https://github.com/octo/repo')"
        ),
        {"id": repository_id, "installation_id": installation_id},
    )
    connection.execute(
        text(
            "INSERT INTO prompt_versions "
            "(id, key, version, content, checksum, is_active) "
            "VALUES (:id, 'review.system', 1, 'system', :checksum, true)"
        ),
        {"id": prompt_id, "checksum": "c" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO rule_versions "
            "(id, repository_id, version, rules, checksum, is_active) "
            "VALUES (:id, :repository_id, 1, '[]'::jsonb, :checksum, true)"
        ),
        {"id": rule_id, "repository_id": repository_id, "checksum": "d" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO code_changes "
            "(id, repository_id, external_id, external_number, title, description, "
            "source_branch, target_branch, base_sha, head_sha, state, web_url, "
            "reviewer_requested, reviewer_requested_at, head_first_seen_at, provider_updated_at) "
            "VALUES (:id, :repository_id, 901, 7, 'Historic title', 'Historic body', "
            "'feature', 'main', :base_sha, :head_sha, 'open', "
            "'https://github.com/octo/repo/pull/7', true, :now, :now, :now)"
        ),
        {
            "id": pr_id,
            "repository_id": repository_id,
            "base_sha": _BASE,
            "head_sha": _HEAD,
            "now": _NOW,
        },
    )
    connection.execute(
        text(
            "INSERT INTO runs "
            "(id, code_change_id, base_sha, base_ref, head_sha, state, trigger, "
            "idempotency_key, engine, rule_version_id, prompt_version_id, attempt, available_at) "
            "VALUES (:id, :pr_id, :base_sha, 'main', :head_sha, 'queued', 'webhook', "
            ":key, 'fast', :rule_id, :prompt_id, 0, :now)"
        ),
        {
            "id": run_id,
            "pr_id": pr_id,
            "base_sha": _BASE,
            "head_sha": _HEAD,
            "key": "e" * 64,
            "rule_id": rule_id,
            "prompt_id": prompt_id,
            "now": _NOW,
        },
    )
    connection.commit()

    command.upgrade(config, "head")
    row = connection.execute(
        text(
            "SELECT title, description, reviewer_requested, ai_review_labeled, "
            "ai_review_labeled_at, label_intent_updated_at, head_first_seen_at "
            "FROM code_changes WHERE id = :id"
        ),
        {"id": pr_id},
    ).one()
    assert (row.title, row.description, row.reviewer_requested) == (
        "Historic title",
        "Historic body",
        True,
    )
    assert row.ai_review_labeled is False
    assert row.ai_review_labeled_at is None
    assert row.label_intent_updated_at is None
    assert row.head_first_seen_at == _NOW
    assert (
        connection.execute(text("SELECT id FROM runs WHERE id = :id"), {"id": run_id}).scalar_one()
        == run_id
    )
    connection.commit()

    async def exercise_store() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        event = PullRequestEvent(
            action="edited",
            installation_external_id=17,
            repository_external_id=101,
            external_id=901,
            number=7,
            title="Historic title",
            description="Historic body",
            author_login="alice",
            web_url="https://github.com/octo/repo/pull/7",
            source_branch="feature",
            target_branch="main",
            base_sha=_BASE,
            head_sha=_HEAD,
            state=PullRequestState.OPEN,
            provider_updated_at=_NOW,
        )
        try:
            async with SqlAlchemyPullRequestProjectionUnitOfWork(sessions) as uow:
                locked = await uow.pull_requests.get_or_create_locked(event, _NOW)
                assert locked is not None and not locked.created
                assert locked.record.ai_review_labeled is False
                locked.record.ai_review_labeled = True
                locked.record.ai_review_labeled_at = _NOW
                locked.record.label_intent_updated_at = _NOW
                await uow.pull_requests.save(locked.record)
                await uow.commit()
            async with sessions() as session:
                persisted = await session.scalar(select(CodeChange).where(CodeChange.id == pr_id))
                assert persisted is not None
                assert persisted.ai_review_labeled is True
                assert persisted.ai_review_labeled_at == _NOW
                assert persisted.label_intent_updated_at == _NOW
                assert persisted.head_first_seen_at == _NOW
        finally:
            await engine.dispose()

    asyncio.run(exercise_store())
