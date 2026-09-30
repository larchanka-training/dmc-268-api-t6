"""REST for the frontend against PostgreSQL and RabbitMQ (#34 PR 2).

Opt-in: ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL``
(disposable RabbitMQ: the tests delete and redeclare the review topology).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.bootstrap.run_update_listener import listen_forever
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.list_pulls import ListRepositoryPulls
from app.modules.reviews.application.run_events import InMemoryRunUpdateHub, RunUpdated
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher, amqp_channels, run_queue
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from app.modules.reviews.infrastructure.run_action_payloads import ROW_LIMIT_BYTES
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.test_worker_queue_postgres import _reset_topology

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
    rabbitmq_url: str

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
            "created_at) VALUES (:id, :pr, :base, 'main', :head, :state, 'webhook', :key, 'fast', "
            ":rule, :prompt, :now, :attempt, :now)",
            id=run_id,
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


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL")
    if database_url is None or rabbitmq_url is None:
        pytest.skip("set TEST_DATABASE_URL and TEST_RABBITMQ_URL to run REST integration tests")
    asyncio.run(_reset_topology(rabbitmq_url))
    schema = f"test_rest_{UUID(int=NOW.microsecond).hex[-8:]}_{os.getpid()}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            _seed(connection)
            connection.commit()
            yield Env(database_url, schema, rabbitmq_url)
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()
        asyncio.run(_reset_topology(rabbitmq_url))


@contextlib.contextmanager
def api(env: Env, scope: AuthScope) -> Iterator[tuple[TestClient, async_sessionmaker[Any]]]:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    publisher = LazyAmqpPublisher(env.rabbitmq_url)
    app.dependency_overrides[get_auth_scope] = lambda: scope
    try:
        # One portal loop for every request: the lazy AMQP connection lives in it.
        with TestClient(app) as client:
            app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
            app.state.run_publisher = publisher
            try:
                yield client, factory
            finally:
                assert client.portal is not None
                client.portal.call(publisher.aclose)
                del app.state.reviews_api_resources
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


async def _queued_messages(url: str) -> list[tuple[int | None, dict[str, Any]]]:
    messages: list[tuple[int | None, dict[str, Any]]] = []
    async with amqp_channels(url, RetryDelays()) as channels:
        queue = await channels.consumer_queue(run_queue("fast"))
        while (message := await queue.get(no_ack=True, fail=False)) is not None:
            messages.append((message.priority, json.loads(message.body)))
    return messages


def _scalar(factory: async_sessionmaker[Any], sql: str, **values: Any) -> Any:
    async def read() -> Any:
        async with factory() as session:
            return (await session.execute(text(sql), values)).one()

    return asyncio.run(read())


@pytest.mark.integration
def test_repositories_and_pulls_are_scoped_to_the_claimed_workspace(env: Env) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        repos = client.get("/api/repos").json()
        other = client.get(f"/api/repos/{REPO_B}")
        patched = client.patch(
            f"/api/repos/{REPO_A}", json={"waitForCi": "never", "maxComments": 4}
        )
        patch_other = client.patch(f"/api/repos/{REPO_B}", json={"enabled": False})
        pulls = client.get(f"/api/repos/{REPO_A}/pulls").json()
        closed = client.get(f"/api/repos/{REPO_A}/pulls", params={"state": "closed"}).json()
        pulls_other = client.get(f"/api/repos/{REPO_B}/pulls")
        stored = _scalar(
            factory, "SELECT wait_for_ci, max_comments FROM repositories WHERE id = :id", id=REPO_A
        )
        untouched = _scalar(factory, "SELECT enabled FROM repositories WHERE id = :id", id=REPO_B)

        async def paged() -> list[list[int]]:
            use_case = ListRepositoryPulls(
                SqlAlchemyPullRequestQueries(factory, AuthScope(42, (WS_A,)))
            )
            first = await use_case.execute(REPO_A, state="all", limit=2)
            assert first is not None and first.next_cursor is not None
            second = await use_case.execute(REPO_A, state="all", cursor=first.next_cursor, limit=2)
            assert second is not None and second.next_cursor is None
            return [[item.number for item in page.items] for page in (first, second)]

        pages = asyncio.run(paged())

    assert [item["id"] for item in repos] == [str(REPO_A)]
    assert repos[0]["reviewEvent"] == "COMMENT"
    assert other.status_code == 404 and patch_other.status_code == 404
    assert patched.status_code == 200
    assert tuple(stored) == ("never", 4) and tuple(untouched) == (True,)
    assert [item["number"] for item in pulls["items"]] == [3, 1]
    assert pulls["items"][1]["latestRun"] == {
        "id": str(RUN_DONE),
        "status": "succeeded",
        "verdict": "attention",
    }
    assert pulls["items"][1]["author"] == "octocat"
    assert [item["number"] for item in closed["items"]] == [2]
    assert pulls_other.status_code == 404
    assert pages == [[3, 2], [1]]


@pytest.mark.integration
def test_rerun_creates_a_priority_nine_run_and_rejects_active_or_closed_prs(env: Env) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        created = client.post(f"/api/runs/{RUN_DONE}/rerun")
        again = client.post(f"/api/runs/{RUN_DONE}/rerun")
        closed = client.post(f"/api/runs/{RUN_CLOSED}/rerun")
        foreign = client.post(f"/api/runs/{RUN_B}/rerun")
        new_id = created.json()["id"]
        row = _scalar(
            factory,
            "SELECT trigger, state, attempt, head_sha, message_published_at IS NOT NULL "
            "FROM runs WHERE id = :id",
            id=UUID(new_id),
        )
        runs = _scalar(factory, "SELECT count(*) FROM runs")

    messages = asyncio.run(_queued_messages(env.rabbitmq_url))

    assert created.status_code == 202 and created.json()["status"] == "queued"
    assert tuple(row) == ("rerun", "queued", 0, HEAD, True)
    assert again.status_code == 409 and closed.status_code == 409
    assert foreign.status_code == 404
    assert tuple(runs) == (5,)
    assert [(priority, body["run_id"], body["trigger"]) for priority, body in messages] == [
        (9, new_id, "rerun")
    ]


@pytest.mark.integration
def test_cancel_of_an_attempted_queued_run_publishes_the_t6_close_signal(env: Env) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        response = client.post(f"/api/runs/{RUN_ATTEMPTED}/cancel")
        row = _scalar(
            factory,
            "SELECT state, error_code, cancellation_signal_published_at IS NOT NULL "
            "FROM runs WHERE id = :id",
            id=RUN_ATTEMPTED,
        )

    messages = asyncio.run(_queued_messages(env.rabbitmq_url))

    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert tuple(row) == ("cancelled", "cancelled_by_user", True)
    assert [(priority, body["run_id"], body["attempt"]) for priority, body in messages] == [
        (9, str(RUN_ATTEMPTED), 2)
    ]


@pytest.mark.integration
def test_large_action_responses_are_stored_by_reference_and_truncated_over_the_limit(
    env: Env,
) -> None:
    large = {"text": "x" * (70 * 1024)}
    huge = {"text": "y" * (ROW_LIMIT_BYTES + 10)}
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
        for response in ({"small": True}, large, huge):
            asyncio.run(trace.record(RUN_DONE, "context.build", {}, response, NOW, 1))
        actions = client.get(f"/api/runs/{RUN_DONE}/actions").json()
        bodies = [
            client.get(f"/api/runs/{RUN_DONE}/actions/{index}/response").json()
            for index in (0, 1, 2)
        ]
        stored = _scalar(
            factory,
            "SELECT count(*), bool_and(response IS NULL) FROM run_actions "
            "WHERE run_id = :id AND response_ref IS NOT NULL",
            id=RUN_DONE,
        )

    assert tuple(stored) == (2, True)
    assert actions[0]["response"] == {"small": True} and actions[0]["responseRef"] is None
    assert [action["responseRef"] for action in actions[1:]] == [
        f"/api/runs/{RUN_DONE}/actions/1/response",
        f"/api/runs/{RUN_DONE}/actions/2/response",
    ]
    assert bodies[1] == large
    assert bodies[2]["truncated"] is True
    assert bodies[2]["original_bytes"] == len(json.dumps(huge, separators=(",", ":")).encode())
    assert huge["text"].startswith(bodies[2]["text"][len('{"text":"') :])


@pytest.mark.integration
def test_worker_status_change_reaches_the_sse_hub_through_listen_notify(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    hub = InMemoryRunUpdateHub()
    database_url = f"{env.database_url}?options=-csearch_path%3D{env.schema}"

    async def scenario() -> list[RunUpdated]:
        received: list[RunUpdated] = []
        async with hub.subscribe() as events:
            listener = asyncio.create_task(listen_forever(database_url, hub))
            await asyncio.sleep(0.5)
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                await uow.runs.finish(
                    RUN_ATTEMPTED,
                    from_state="queued",
                    worker_id=None,
                    state="skipped",
                    error_code="repo_disabled",
                    error_message=None,
                    now=NOW,
                )
                await uow.rollback()
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                await uow.runs.claim(
                    RUN_ATTEMPTED, worker_id="w", now=NOW, lease_until=NOW + timedelta(minutes=5)
                )
                await uow.commit()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    while True:
                        received.append(await anext(events))
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        await engine.dispose()
        return received

    assert asyncio.run(scenario()) == [RunUpdated(RUN_ATTEMPTED, "running")]
