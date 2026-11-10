"""Shared PostgreSQL and RabbitMQ setup of the portal REST integration tests (#34).

Opt-in: ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL``
(disposable RabbitMQ: the tests delete and redeclare the review topology). Two
Workspaces: user 42 is granted repository A of Workspace A and repository B of B.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher, amqp_channels, run_queue
from tests.template_database import migrated_database
from tests.worker_queue_postgres import _reset_topology

WS_A = UUID("00000000-0000-4000-8000-00000000000a")
WS_B = UUID("00000000-0000-4000-8000-00000000000b")
INSTALL_A = UUID("00000000-0000-4000-8000-0000000000a1")
INSTALL_B = UUID("00000000-0000-4000-8000-0000000000b1")
REPO_A = UUID("00000000-0000-4000-8000-0000000000a2")
REPO_B = UUID("00000000-0000-4000-8000-0000000000b2")
PR_OPEN = UUID("00000000-0000-4000-8000-0000000000a3")
PR_CLOSED = UUID("00000000-0000-4000-8000-0000000000a4")
PR_NEW = UUID("00000000-0000-4000-8000-0000000000a5")
PR_B = UUID("00000000-0000-4000-8000-0000000000b3")
RUN_DONE = UUID("00000000-0000-4000-8000-0000000000a6")
RUN_CLOSED = UUID("00000000-0000-4000-8000-0000000000a7")
RUN_ATTEMPTED = UUID("00000000-0000-4000-8000-0000000000a8")
RUN_B = UUID("00000000-0000-4000-8000-0000000000b4")
RULE_A = UUID("00000000-0000-4000-8000-0000000000a9")
RULE_B = UUID("00000000-0000-4000-8000-0000000000b9")
PROMPT = UUID("00000000-0000-4000-8000-0000000000c1")
HEAD = "e" * 40
NOW = datetime.now(UTC)


@dataclass(frozen=True)
class Env:
    database_url: str
    schema: str
    rabbitmq_url: str | None

    def engine(self) -> AsyncEngine:
        return create_async_engine(
            self.database_url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )


def _seed(connection: Any) -> None:
    def run(statement: str, **values: Any) -> None:
        connection.execute(text(statement), values)

    for workspace in (WS_A, WS_B):
        run(
            "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'W', 0)", id=workspace
        )
    for installation, workspace, external in ((INSTALL_A, WS_A, 17), (INSTALL_B, WS_B, 18)):
        run(
            "INSERT INTO provider_installations (id, workspace_id, provider, external_id, "
            "metadata) VALUES (:id, :ws, 'github', :ext, '{}'::jsonb)",
            id=installation,
            ws=workspace,
            ext=external,
        )
    run("INSERT INTO github_user_profiles (id, login, name) VALUES (42, 'alice', 'alice')")
    for workspace in (WS_A, WS_B):
        run(
            "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
            "VALUES (42, :ws)",
            ws=workspace,
        )
    for installation, external in ((INSTALL_A, 101), (INSTALL_B, 102)):
        run(
            "INSERT INTO github_user_repository_access (github_user_id, provider_installation_id, "
            "repository_external_id) VALUES (42, :installation, :ext)",
            installation=installation,
            ext=external,
        )
    run(
        "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
        "VALUES (:id, 'review.system', 1, 'system', :checksum, true)",
        id=PROMPT,
        checksum="c" * 64,
    )
    for repo, installation, external, rule, name in (
        (REPO_A, INSTALL_A, 101, RULE_A, "octo/a"),
        (REPO_B, INSTALL_B, 102, RULE_B, "octo/b"),
    ):
        run(
            "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
            "default_branch, web_url) VALUES (:id, :installation, :ext, :name, 'main', :url)",
            id=repo,
            installation=installation,
            ext=external,
            name=name,
            url=f"https://github.test/{name}",
        )
        run(
            "INSERT INTO rule_versions (id, repository_id, version, rules, checksum, is_active) "
            "VALUES (:id, :repo, 1, '[]'::jsonb, :checksum, true)",
            id=rule,
            repo=repo,
            checksum=str(external)[-1] * 64,
        )
    for pr, repo, number, state, minutes in (
        (PR_OPEN, REPO_A, 1, "open", 3),
        (PR_CLOSED, REPO_A, 2, "closed", 2),
        (PR_NEW, REPO_A, 3, "open", 1),
        (PR_B, REPO_B, 1, "open", 1),
    ):
        run(
            "INSERT INTO code_changes (id, repository_id, external_id, external_number, title, "
            "author_login, source_branch, target_branch, base_sha, head_sha, state, web_url, "
            "provider_updated_at) VALUES (:id, :repo, :ext, :number, 'PR', 'octocat', "
            "'feature', 'main', :base, :head, :state, :url, :updated)",
            id=pr,
            repo=repo,
            ext=900 + number,
            number=number,
            base="b" * 40,
            head=HEAD,
            state=state,
            url=f"https://github.test/pull/{number}",
            updated=NOW - timedelta(minutes=minutes),
        )
    for run_id, pr, rule, state, attempt, key in (
        (RUN_DONE, PR_OPEN, RULE_A, "succeeded", 1, "1"),
        (RUN_CLOSED, PR_CLOSED, RULE_A, "succeeded", 1, "2"),
        (RUN_ATTEMPTED, PR_NEW, RULE_A, "queued", 1, "3"),
        (RUN_B, PR_B, RULE_B, "succeeded", 1, "4"),
    ):
        run(
            "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, state, trigger, "
            "idempotency_key, engine, rule_version_id, prompt_version_id, available_at, attempt, "
            "created_at, origin_provider_installation_id) VALUES (:id, :pr, "
            ":base, 'main', :head, :state, 'webhook', :key, 'fast', "
            ":rule, :prompt, :now, :attempt, :now, :origin)",
            id=run_id,
            origin=INSTALL_B if run_id == RUN_B else INSTALL_A,
            pr=pr,
            base="b" * 40,
            head=HEAD,
            state=state,
            key=key * 64,
            rule=rule,
            prompt=PROMPT,
            now=NOW,
            attempt=attempt,
        )
    run(
        "INSERT INTO findings (id, run_id, file_path, line_start, severity, confidence, category, "
        "title, body, published, inline_comment) VALUES (:id, :run, 'a.py', 3, 'medium', 0.8, "
        "'correctness', 'T', 'B', true, true)",
        id=UUID(int=77),
        run=RUN_DONE,
    )


@contextlib.contextmanager
def portal_schema(database_url: str, rabbitmq_url: str | None) -> Iterator[Env]:
    """A migrated, seeded schema; the review topology is reset only with a broker URL.

    Each test file's fixture decides what it needs and skips without it.
    """
    if rabbitmq_url is not None:
        asyncio.run(_reset_topology(rabbitmq_url))
    schema = f"test_rest_{uuid4().hex}"
    try:
        with migrated_database(database_url, schema) as clone_url:
            engine = create_engine(clone_url, poolclass=NullPool)
            try:
                with engine.begin() as connection:
                    connection.execute(text(f'SET search_path TO "{schema}"'))
                    _seed(connection)
            finally:
                engine.dispose()
            yield Env(clone_url, schema, rabbitmq_url)
    finally:
        if rabbitmq_url is not None:
            asyncio.run(_reset_topology(rabbitmq_url))


@pytest.fixture
def portal_env() -> Iterator[Env]:
    """The seeded schema without a broker; skips without ``TEST_DATABASE_URL``."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


@contextlib.contextmanager
def api(env: Env, scope: AuthScope) -> Iterator[tuple[TestClient, async_sessionmaker[Any]]]:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Without a broker the API skips its publications, as in production.
    publisher = LazyAmqpPublisher(env.rabbitmq_url) if env.rabbitmq_url else None
    app.dependency_overrides[get_auth_scope] = lambda: scope
    try:
        # One portal loop for every request: the lazy AMQP connection lives in it.
        with TestClient(app) as client:
            app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
            if publisher is not None:
                app.state.run_publisher = publisher
            try:
                yield client, factory
            finally:
                if publisher is not None:
                    assert client.portal is not None
                    client.portal.call(publisher.aclose)
                del app.state.reviews_api_resources
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


async def queued_messages(env: Env) -> list[tuple[int | None, dict[str, Any]]]:
    assert env.rabbitmq_url is not None, "publication tests need TEST_RABBITMQ_URL"
    messages: list[tuple[int | None, dict[str, Any]]] = []
    async with amqp_channels(env.rabbitmq_url, RetryDelays()) as channels:
        queue = await channels.consumer_queue(run_queue("fast"))
        while (message := await queue.get(no_ack=True, fail=False)) is not None:
            messages.append((message.priority, json.loads(message.body)))
    return messages


def scalar(factory: async_sessionmaker[Any], sql: str, **values: Any) -> Any:
    async def read() -> Any:
        async with factory() as session:
            return (await session.execute(text(sql), values)).one()

    return asyncio.run(read())


def clean_review_result(head: str) -> str:
    """The body of a ``review.postprocess`` action of a clean, completely covered run."""
    return json.dumps(
        {
            "severity_counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "verdict": "clean",
            "summary": {
                "problem": "Подтверждённых замечаний нет.",
                "done_well": "Проанализировано файлов с текстовыми изменениями: 1; "
                "исключено файлов: 0.",
                "effort": "none",
            },
            "coverage": {
                "version": 1,
                "head_sha": head,
                "diff_digest": "a" * 64,
                "snapshot_digest": "b" * 64,
                "file_list_complete": True,
                "total_files": 1,
                "reviewed_files": 1,
                "status": "complete",
                "exclusion_counts": {},
                "reason_counts": {},
                "exclusions": [],
                "reasons": [],
            },
        }
    )


async def copy_seeded_run(
    session: object,
    run_id: UUID,
    code_change_id: UUID,
    state: str,
    created_at: datetime,
    finished_at: datetime,
    idempotency_key: str,
) -> None:
    """A Run on ``code_change_id`` with the first seeded Run's refs, engine and versions."""
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, state, trigger, "
            "idempotency_key, engine, rule_version_id, prompt_version_id, available_at, attempt, "
            "created_at, finished_at, origin_provider_installation_id) "
            "SELECT :id, :pr, base_sha, base_ref, head_sha, :state, 'manual', :key, engine, "
            "rule_version_id, prompt_version_id, :created, 1, :created, :finished, "
            "origin_provider_installation_id FROM runs LIMIT 1"
        ),
        {
            "id": run_id,
            "pr": code_change_id,
            "state": state,
            "key": idempotency_key,
            "created": created_at,
            "finished": finished_at,
        },
    )
