"""Worker against real RabbitMQ and PostgreSQL with fake GitHub and ReviewModel (#34).

Opt-in: set ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL``
(disposable RabbitMQ: the tests delete and redeclare the review topology).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
import os
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import httpx
import psycopg
import pytest
from aiormq.exceptions import ChannelInvalidStateError, ChannelPreconditionFailed
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import app.modules.code_index.infrastructure.amqp as index_amqp
import app.worker as worker_module
from alembic import command
from app.bootstrap.reviews_api import get_run_repository
from app.common.infrastructure.db.leader import run_as_leader
from app.common.infrastructure.heartbeat import is_fresh
from app.main import app
from app.modules.reviews.application.check_runs import (
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.determine_ci_eligibility import CiEligibility
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
from app.modules.reviews.application.publish_run_review import ReviewSubmission, SubmittedReview
from app.modules.reviews.application.queue_messages import StoredRunMessage
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_delivery import HandleRunDelivery, RunDeliveryPointer
from app.modules.reviews.application.run_failures import RetryDelays, RunFailure
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
)
from app.modules.reviews.infrastructure.amqp import (
    DEAD_LETTER_EXCHANGE,
    DEAD_LETTER_QUEUE,
    ENGINES,
    EXCHANGE,
    PUBLISH_QUEUE,
    RETRY_EXCHANGE,
    RETRY_KEYS,
    AmqpChannels,
    LazyAmqpPublisher,
    amqp_channels,
    consume,
    handle_publish_delivery,
    handle_run_delivery,
    retry_queue,
    run_queue,
)
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.run_delivery import SqlAlchemyRunDeliveryReader
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import GitHubAdapters, WorkerSettings, compose_worker_process, run_worker
from tests import test_gitlab_projection_postgres as projection
from tests.portal_test_client import authenticated_test_client
from tests.test_gitlab_projection_postgres import rig
from tests.test_run_delivery_postgres import seeded

guard_database = projection.authority_database

HEAD = "e" * 40
BASE = "b" * 40
PATCH = "@@ -10,2 +10,3 @@\n line10\n+line11\n line12"
SHORT_DELAYS = RetryDelays(
    short=timedelta(milliseconds=200),
    medium=timedelta(milliseconds=300),
    long=timedelta(milliseconds=400),
)


@dataclass(frozen=True)
class Env:
    installation_id: UUID
    database_url: str
    schema: str
    rabbitmq_url: str
    pr_id: UUID
    rule_id: UUID
    prompt_id: UUID

    def engine(self, role: str = "") -> AsyncEngine:
        # application_name tells this test's sessions apart in pg_stat_activity.
        return create_async_engine(
            self.database_url,
            connect_args={
                "options": f"-csearch_path={self.schema}",
                "application_name": self.schema + role,
            },
            poolclass=NullPool,
        )


async def _reset_topology(url: str) -> None:
    connection = await aio_pika.connect(url)
    try:
        channel = await connection.channel()
        names = [DEAD_LETTER_QUEUE, PUBLISH_QUEUE, index_amqp.DEAD_LETTER_QUEUE, index_amqp.QUEUE]
        for engine in ENGINES:
            names.append(run_queue(engine))
            names.extend(retry_queue(key, engine) for key in RETRY_KEYS)
        # The worker declares the index.repo topology too (#142).
        names.extend(index_amqp.retry_queue(key) for key in RETRY_KEYS)
        for name in names:
            await channel.queue_delete(name)
        for exchange in (
            EXCHANGE,
            RETRY_EXCHANGE,
            DEAD_LETTER_EXCHANGE,
            index_amqp.EXCHANGE,
            index_amqp.RETRY_EXCHANGE,
            index_amqp.DEAD_LETTER_EXCHANGE,
        ):
            await channel.exchange_delete(exchange)
    finally:
        await connection.close()


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL") or (
        os.environ.get("RABBITMQ_URL") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    )
    if database_url is None or rabbitmq_url is None:
        pytest.skip("set TEST_DATABASE_URL and TEST_RABBITMQ_URL to run worker integration tests")
    asyncio.run(_reset_topology(rabbitmq_url))
    schema = f"test_worker_{uuid4().hex}"
    workspace_id, installation_id, repository_id = uuid4(), uuid4(), uuid4()
    pr_id, rule_id, prompt_id = uuid4(), uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            statements: list[tuple[str, dict[str, Any]]] = [
                (
                    "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'W', 0)",
                    {"id": workspace_id},
                ),
                (
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :workspace, 'github', 17, '{}'::jsonb)",
                    {"id": installation_id, "workspace": workspace_id},
                ),
                (
                    "INSERT INTO repositories "
                    "(id, provider_installation_id, external_id, full_name, default_branch, "
                    "web_url) VALUES (:id, :installation, 101, 'octo/repo', 'main', "
                    "'https://github.test/octo/repo')",
                    {"id": repository_id, "installation": installation_id},
                ),
                (
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                    "VALUES (:id, 'review.system', 1, 'system', :checksum, true)",
                    {"id": prompt_id, "checksum": "c" * 64},
                ),
                (
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                    "VALUES (:id, 'review.conventions', 1, 'conventions', :checksum, true)",
                    {"id": uuid4(), "checksum": "d" * 64},
                ),
                (
                    "INSERT INTO rule_versions "
                    "(id, repository_id, version, rules, checksum, is_active) "
                    "VALUES (:id, :repository, 1, '[]'::jsonb, :checksum, true)",
                    {"id": rule_id, "repository": repository_id, "checksum": "a" * 64},
                ),
                (
                    "INSERT INTO code_changes "
                    "(id, repository_id, external_id, external_number, title, source_branch, "
                    "target_branch, base_sha, head_sha, state, web_url, ai_review_labeled) "
                    "VALUES (:id, :repository, 907, 7, 'PR', 'feature', 'main', :base, :head, "
                    "'open', 'https://github.test/octo/repo/pull/7', true)",
                    {"id": pr_id, "repository": repository_id, "base": BASE, "head": HEAD},
                ),
            ]
            for statement, values in statements:
                connection.execute(text(statement), values)
            connection.commit()
            yield Env(
                installation_id, database_url, schema, rabbitmq_url, pr_id, rule_id, prompt_id
            )
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()
        asyncio.run(_reset_topology(rabbitmq_url))


async def insert_run(factory: async_sessionmaker[AsyncSession], env: Env, **values: Any) -> UUID:
    run_id = uuid4()
    row = {
        "id": run_id,
        "origin": env.installation_id,
        "pr": env.pr_id,
        "base": BASE,
        "head": HEAD,
        "state": "queued",
        "trigger": "webhook",
        "key": uuid4().hex + uuid4().hex,
        "rule": env.rule_id,
        "prompt": env.prompt_id,
        "available": datetime.now(UTC),
        "attempt": 0,
        "lease": None,
    }
    row.update(values)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, state, "
                "trigger, idempotency_key, engine, rule_version_id, prompt_version_id, "
                "available_at, attempt, lease_until, "
                "origin_provider_installation_id) VALUES (:id, :pr, :base, "
                "'main', :head, "
                ":state, :trigger, :key, 'fast', :rule, :prompt, :available, "
                ":attempt, :lease, :origin)"
            ),
            row,
        )
    return run_id


def review_output() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": "app/example.py",
                "line": 11,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Inverted branch",
                "body": "The new line inverts the branch.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {
            "problem": "The branch is inverted.",
            "done_well": "The change is small.",
            "effort": "small",
        },
    }


@dataclass
class Model:
    """Fake provider for the whole ReviewWorkerProvider port."""

    factory: async_sessionmaker[AsyncSession]
    fail: bool = False
    hang: bool = False
    before_review: Callable[[], Awaitable[None]] | None = None
    review_calls: int = 0
    deadlines: list[datetime] = field(default_factory=list)
    observed: list[tuple[str, bool, str | None, int]] = field(default_factory=list)
    open_transactions: list[int] = field(default_factory=list)

    def for_attempt(self, claimed: ClaimedAttempt) -> Model:
        self.deadlines.append(claimed.deadline)
        return self

    async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]:
        raise AssertionError("the worker uses the VCS provider")

    async def fetch_file_content(self, *, code_change_id: UUID, head_sha: str, path: str) -> str:
        raise AssertionError("the worker uses the VCS provider")

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return RepositorySnapshot(None, None)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return (RepositoryFile("app/example.py", 10),)

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return tuple(RepositoryFile(path, 10, "x = 1\n") for path in paths)

    async def draft_conventions(self, *, request: ConventionsRequest) -> Mapping[str, object]:
        return {
            "files": [{"path": "app/example.py", "relevance": "Changed module."}],
            "key_patterns": ["Pattern one.", "Pattern two.", "Pattern three."],
            "recommendations": [
                f"Recommendation {n} (from: standard/correctness)"
                for n in ("one", "two", "three", "four", "five")
            ],
        }

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        if self.before_review is not None:
            await self.before_review()
        return PullRequestMeta(
            title="PR",
            description=None,
            author="octocat",
            source_branch="feature",
            target_branch="main",
            labels=("ai-review",),
            files_changed=1,
            lines_added=1,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        )

    async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object] | str | bytes:
        self.review_calls += 1
        async with self.factory() as session:
            row = (
                await session.execute(
                    text("SELECT state, lease_until IS NOT NULL, worker_id, attempt FROM runs")
                )
            ).one()
            self.observed.append((str(row[0]), bool(row[1]), row[2], int(row[3])))
            self.open_transactions.append(
                int(
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                            "current_database() AND application_name = "
                            "current_setting('application_name') "
                            "AND state LIKE 'idle in transaction%'"
                        )
                    )
                    or 0
                )
            )
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise RunFailure("llm_unavailable", "provider is down")
        return review_output()

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        raise AssertionError("publication goes through review.publish")


class Vcs:
    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        await checkpoint()
        return VcsPullRequest(
            locator=locator,
            head_sha=HEAD,
            base_sha=BASE,
            meta=PullRequestMeta(
                title="PR",
                description=None,
                author="octocat",
                source_branch="feature",
                target_branch="main",
                labels=("ai-review",),
                files_changed=1,
                lines_added=1,
                lines_removed=0,
                is_draft=False,
                is_fork=False,
            ),
        )

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return (
            VcsFile(
                filename="app/example.py",
                status="modified",
                blob_sha="f" * 40,
                previous_filename=None,
                additions=1,
                deletions=0,
                changes=1,
                patch=PATCH,
                size=30,
            ),
        )

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        return b"line10\nline11\nline12\n"


@dataclass
class GitHub:
    check_runs: list[tuple[UUID, str, str | None]] = field(default_factory=list)
    reviews: list[ReviewSubmission] = field(default_factory=list)
    during_post: Callable[[], Awaitable[None]] | None = None

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        await checkpoint()
        self.check_runs.append((target.run_id, view.status, view.conclusion))

    async def submit_review(
        self, submission: ReviewSubmission, *, checkpoint: RunProviderCheckpoint
    ) -> SubmittedReview:
        await checkpoint()
        self.reviews.append(submission)
        if self.during_post is not None:
            await self.during_post()
        return SubmittedReview(7001, tuple(9000 + n for n in range(len(submission.findings))))

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        raise AssertionError("eligibility is not used by these tests")


SETTINGS = WorkerSettings(
    database_url="unused",
    rabbitmq_url="unused",
    worker_id="worker-it",
    github_app_id="1",
    github_private_key="unused",
    github_api_url="https://api.github.test",
    portal_url="https://portal.test",
)


def _no_run_source(run_id: UUID) -> GitHubRunSource:
    raise AssertionError("these tests inject provider_factory")


@contextlib.asynccontextmanager
async def running_worker(
    env: Env,
    factory: async_sessionmaker[AsyncSession],
    model: Model,
    github: GitHub,
    channels: AmqpChannels,
) -> AsyncIterator[None]:
    process = compose_worker_process(
        settings=SETTINGS,
        session_factory=factory,
        queue=channels.publisher,
        github=GitHubAdapters(
            vcs=Vcs(),
            check_runs=github,
            reviews=github,
            eligibility=github,
            run_source=_no_run_source,
        ),
        provider_factory=model.for_attempt,
        delays=SHORT_DELAYS,
    )
    run_q = await channels.consumer_queue(run_queue("fast"))
    publish_q = await channels.consumer_queue(PUBLISH_QUEUE)
    tasks = [
        asyncio.create_task(
            consume(
                run_q,
                partial(
                    handle_run_delivery,
                    handler=process.handle_delivery.execute,
                    publisher=channels.publisher,
                ),
            )
        ),
        asyncio.create_task(
            consume(
                publish_q,
                partial(
                    handle_publish_delivery,
                    handler=process.publish_review.execute,
                    publisher=channels.publisher,
                ),
            )
        ),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await run_q.channel.close()
        await publish_q.channel.close()


async def run_state(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> tuple[Any, ...]:
    async with factory() as session:
        return tuple(
            (
                await session.execute(
                    text("SELECT state, attempt, error_code FROM runs WHERE id = :id"),
                    {"id": run_id},
                )
            ).one()
        )


async def wait_for_state(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, *states: str, timeout: float = 30
) -> tuple[Any, ...]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        current = await run_state(factory, run_id)
        if current[0] in states:
            return current
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run {run_id} stayed {current}")
        await asyncio.sleep(0.05)


async def wait_for_check_run(github: GitHub, status: str, timeout: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not any(item[1] == status for item in github.check_runs):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no {status} check-run in {github.check_runs}")
        await asyncio.sleep(0.05)


async def publish_run(
    channels: AmqpChannels, factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> None:
    async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
        message = await uow.runs.run_message(run_id)
    assert message is not None
    await channels.publisher.publish_confirmed(message)


@contextlib.asynccontextmanager
async def listen(env: Env) -> AsyncIterator[list[dict[str, str]]]:
    events: list[dict[str, str]] = []
    connection = await psycopg.AsyncConnection.connect(
        env.database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    )
    await connection.execute("LISTEN run_updated")

    async def collect() -> None:
        async for notify in connection.notifies():
            events.append(json.loads(notify.payload))

    task = asyncio.create_task(collect())
    try:
        yield events
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await connection.close()


async def tools(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> list[tuple[Any, ...]]:
    async with factory() as session:
        return [
            tuple(row)
            for row in await session.execute(
                text(
                    "SELECT tool, duration_ms, response FROM run_actions "
                    "WHERE run_id = :id ORDER BY index"
                ),
                {"id": run_id},
            )
        ]


@pytest.mark.integration
def test_full_path_queued_running_publishing_succeeded_is_readable_over_rest(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # The state poller's read transactions stay out of the worker's idle-in-transaction count.
    poll_engine = env.engine("-poll")
    poll_factory = async_sessionmaker(poll_engine, expire_on_commit=False)
    model = Model(factory)
    github = GitHub()

    async def scenario() -> tuple[UUID, list[dict[str, str]], datetime]:
        run_id = await insert_run(factory, env)
        async with (
            listen(env) as events,
            amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels,
            running_worker(env, factory, model, github, channels),
        ):
            await publish_run(channels, factory, run_id)
            claimed_before = datetime.now(UTC)
            await wait_for_state(poll_factory, run_id, "succeeded")
            await wait_for_check_run(github, "completed")
            await asyncio.sleep(0.2)
        await engine.dispose()
        await poll_engine.dispose()
        return run_id, events, claimed_before

    run_id, events, published_at = asyncio.run(scenario())

    async def read() -> tuple[Any, ...]:
        async with factory() as session:
            findings = (
                await session.execute(
                    text("SELECT published, inline_comment FROM findings WHERE run_id = :id"),
                    {"id": run_id},
                )
            ).all()
            comments = await session.scalar(
                text("SELECT count(*) FROM comments"),
            )
            payload = (
                await session.execute(
                    text("SELECT s3_ref, summary FROM context_payloads WHERE run_id = :id"),
                    {"id": run_id},
                )
            ).one()
            run = (
                await session.execute(
                    text("SELECT attempt, worker_id, finished_at IS NOT NULL FROM runs")
                )
            ).one()
        return findings, comments, payload, run, await tools(factory, run_id)

    findings, comments, payload, run, actions = asyncio.run(read())
    asyncio.run(engine.dispose())

    assert [event["status"] for event in events if event["run_id"] == str(run_id)] == [
        "running",
        "publishing",
        "succeeded",
    ]
    assert model.observed == [("running", True, "worker-it", 1)]
    assert model.open_transactions == [0]
    assert model.deadlines[0] - published_at < timedelta(minutes=8, seconds=5)
    assert findings == [(True, True)]
    assert comments == 1
    assert payload[0] is None and payload[1]["files"][0]["level_used"] == "L1"
    assert run == (1, "worker-it", True)
    names = [action[0] for action in actions]
    for tool in ("vcs.fetch_diff", "context.build", "review.postprocess", "github.publish_review"):
        assert names.count(tool) == 1, names
        assert all(action[1] >= 0 for action in actions if action[0] == tool)
    assert names.count("llm.review_output") == 1
    assert names.index("vcs.fetch_diff") < names.index("context.build")
    assert names.index("review.postprocess") < names.index("github.publish_review")
    assert github.reviews[0].commit_sha == HEAD
    assert [(status, conclusion) for _, status, conclusion in github.check_runs] == [
        ("in_progress", None),
        ("completed", "neutral"),
    ]

    app.dependency_overrides[get_run_repository] = lambda: SqlAlchemyRunRepository(
        factory, allow_unscoped=True
    )
    try:
        client = authenticated_test_client(app)
        detail = client.get(f"/api/runs/{run_id}")
        comments_response = client.get(f"/api/runs/{run_id}/comments")
    finally:
        app.dependency_overrides.clear()
    assert detail.status_code == 200 and detail.json()["status"] == "succeeded"
    assert comments_response.status_code == 200
    assert [item["title"] for item in comments_response.json()] == ["Inverted branch"]


@pytest.mark.integration
def test_three_model_failures_retry_through_retry_queues_then_fail_into_the_dlq(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    model = Model(factory, fail=True)
    github = GitHub()

    async def scenario() -> tuple[tuple[Any, ...], dict[str, Any] | None, UUID]:
        run_id = await insert_run(factory, env)
        dead: dict[str, Any] | None = None
        async with (
            amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels,
            running_worker(env, factory, model, github, channels),
        ):
            await publish_run(channels, factory, run_id)
            state = await wait_for_state(factory, run_id, "failed")
            await wait_for_check_run(github, "completed")
            dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)
            for _ in range(100):
                message = await dlq.get(no_ack=True, fail=False)
                if message is not None:
                    dead = json.loads(message.body)
                    break
                await asyncio.sleep(0.1)
        await engine.dispose()
        return state, dead, run_id

    state, dead, run_id = asyncio.run(scenario())

    assert state == ("failed", 3, "llm_unavailable")
    assert model.review_calls == 3
    assert dead is not None and dead["run_id"] == str(run_id)
    assert [conclusion for _, status, conclusion in github.check_runs if status == "completed"] == [
        "neutral"
    ]


@pytest.mark.integration
def test_worker_killed_mid_run_is_recovered_by_the_reconciler(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    hanging = Model(factory, hang=True)
    healthy = Model(factory)
    github = GitHub()

    async def scenario() -> tuple[tuple[Any, ...], int]:
        run_id = await insert_run(factory, env)
        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            async with running_worker(env, factory, hanging, github, channels):
                await publish_run(channels, factory, run_id)
                await wait_for_state(factory, run_id, "running")
                while hanging.review_calls == 0:
                    await asyncio.sleep(0.05)
            # The worker is gone mid-run; its lease expires.
            async with factory() as session, session.begin():
                await session.execute(
                    text("UPDATE runs SET lease_until = now() - interval '1 second'")
                )
            republished = await ReconcileRuns(
                uow_factory=partial(SqlAlchemyRunLifecycleUnitOfWork, factory),
                run_publisher=channels.publisher,
                review_queue=channels.publisher,
            ).execute()
            async with running_worker(env, factory, healthy, github, channels):
                state = await wait_for_state(factory, run_id, "succeeded")
        await engine.dispose()
        return state, republished

    state, republished = asyncio.run(scenario())

    assert republished == 1
    assert state == ("succeeded", 2, None)
    assert healthy.review_calls == 1


@pytest.mark.integration
def test_new_push_cancels_the_running_run_before_the_model_call(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    github = GitHub()

    async def push() -> None:
        from app.modules.reviews.infrastructure.github_pull_request_projection import (
            SqlAlchemyPullRequestProjectionUnitOfWork,
        )

        async with SqlAlchemyPullRequestProjectionUnitOfWork(factory) as uow:
            await uow.session.execute(
                text("UPDATE code_changes SET head_sha = :sha"), {"sha": "d" * 40}
            )
            await uow.runs.cancel_for_pr(env.pr_id, "superseded", datetime.now(UTC))
            await uow.commit()

    model = Model(factory, before_review=push)

    async def scenario() -> tuple[Any, ...]:
        run_id = await insert_run(factory, env)
        async with (
            amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels,
            running_worker(env, factory, model, github, channels),
        ):
            await publish_run(channels, factory, run_id)
            state = await wait_for_state(factory, run_id, "cancelled")
            await wait_for_check_run(github, "completed")
        await engine.dispose()
        return state

    assert asyncio.run(scenario()) == ("cancelled", 1, "superseded")
    assert model.review_calls == 0
    assert [(status, conclusion) for _, status, conclusion in github.check_runs][-1] == (
        "completed",
        "cancelled",
    )


@pytest.mark.integration
def test_topology_is_declared_with_the_spec_arguments(env: Env) -> None:
    expected: dict[str, dict[str, Any]] = {
        DEAD_LETTER_QUEUE: {"x-message-ttl": 7 * 24 * 60 * 60 * 1000},
        PUBLISH_QUEUE: {
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
        },
    }
    ttls = {"30s": 30_000, "2m": 120_000, "10m": 600_000}
    for engine in ENGINES:
        expected[run_queue(engine)] = {
            "x-max-priority": 10,
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
        }
        for key in RETRY_KEYS:
            expected[retry_queue(key, engine)] = {
                "x-message-ttl": ttls[key],
                "x-dead-letter-exchange": EXCHANGE,
                "x-dead-letter-routing-key": run_queue(engine),
            }

    async def scenario() -> list[str]:
        async with amqp_channels(env.rabbitmq_url, RetryDelays()) as channels:
            mismatched: list[str] = []
            for name, arguments in expected.items():
                channel = await channels.connection.channel()
                # Redeclaring with identical arguments succeeds.
                await channel.declare_queue(name, durable=True, arguments=arguments)
                await channel.close()
                channel = await channels.connection.channel()
                try:
                    await channel.declare_queue(name, durable=True, arguments={})
                    mismatched.append(name)
                except ChannelPreconditionFailed:
                    pass
            return mismatched

    # Every queue rejects a redeclaration without its arguments: they are set as the spec says.
    assert asyncio.run(scenario()) == []
    assert len(expected) == 2 + 2 * (1 + 3)


@pytest.mark.integration
def test_second_leader_does_not_tick_while_the_first_holds_the_lock(env: Env) -> None:
    engine = env.engine()
    ticks = {"first": 0, "second": 0}

    def tick(name: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            ticks[name] += 1

        return run

    # Advisory locks are database-wide: a fixed key collides with a concurrent test run.
    key = uuid4().int >> 65  # 63-bit non-negative bigint (pg_try_advisory_lock takes bigint)

    async def scenario() -> None:
        first = asyncio.create_task(run_as_leader(engine, key, 0.05, tick("first"), name="a"))
        await asyncio.sleep(0.3)
        second = asyncio.create_task(run_as_leader(engine, key, 0.05, tick("second"), name="b"))
        await asyncio.sleep(0.5)
        assert ticks["first"] > 3 and ticks["second"] == 0
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0.5)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        await engine.dispose()

    asyncio.run(scenario())
    # After the leader stops, the second instance takes over.
    assert ticks["second"] > 0


@pytest.mark.integration
def test_worker_without_github_app_starts_and_stays_healthy(
    env: Env, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    database_url = f"{env.database_url}?options=-csearch_path%3D{env.schema}"
    heartbeat = tmp_path / "worker.heartbeat"
    heartbeat.touch()  # left by a previous process: must not count as a beat of this one
    os.utime(heartbeat, (1.0, 1.0))
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": database_url,
            "RABBITMQ_URL": env.rabbitmq_url,
            "WORKER_HEARTBEAT_FILE": str(heartbeat),
        }
    )

    async def scenario() -> bool:
        task = asyncio.create_task(run_worker(settings, leader_period=0.1))
        await asyncio.sleep(1.5)
        healthy = not task.done() and is_fresh(heartbeat, max_age=5, now=time.time())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return healthy

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(scenario()) is True
    assert (
        "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set: the no-CI sweep and "
        "GitHub publication (reviews and check-runs) are disabled"
    ) in caplog.text
    assert (
        "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set: the index.repo consumer is disabled"
    ) in caplog.text
    assert "leader tick failed" not in caplog.text


@pytest.mark.integration
def test_reconciler_store_selects_t12_t13_t17_t18_candidates(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    published: list[tuple[str, UUID]] = []

    class Publisher:
        async def publish_confirmed(self, message: Any, *, kind: Any = None) -> None:
            published.append(("run", message.run_id))

        async def publish_review(self, pointer: Any) -> None:
            published.append(("publish", pointer.run_id))

    numbers = itertools.count(1000)

    async def pr(head: str = HEAD) -> UUID:
        pr_id = uuid4()
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO code_changes (id, repository_id, external_id, external_number, "
                    "title, source_branch, target_branch, base_sha, head_sha, state, web_url) "
                    "SELECT :id, repository_id, :ext, :ext, 'PR', 'f', 'main', base_sha, :head, "
                    "'open', web_url FROM code_changes WHERE id = :template"
                ),
                {"id": pr_id, "ext": next(numbers), "head": head, "template": env.pr_id},
            )
        return pr_id

    async def scenario() -> dict[str, tuple[Any, ...]]:
        expired = now - timedelta(seconds=5)
        runs = {
            "t12": await insert_run(
                factory, env, pr=await pr(), state="running", attempt=1, lease=expired
            ),
            "t13": await insert_run(
                factory, env, pr=await pr(), state="running", attempt=3, lease=expired
            ),
            "t17": await insert_run(
                factory, env, pr=await pr(), state="publishing", attempt=1, lease=expired
            ),
            "t18": await insert_run(
                factory, env, pr=await pr(), available=now - timedelta(minutes=11)
            ),
            "retry": await insert_run(
                factory, env, pr=await pr(), attempt=1, available=now + timedelta(minutes=2)
            ),
            "live": await insert_run(
                factory,
                env,
                pr=await pr(),
                state="running",
                attempt=1,
                lease=now + timedelta(minutes=5),
            ),
        }
        await ReconcileRuns(
            uow_factory=partial(SqlAlchemyRunLifecycleUnitOfWork, factory),
            run_publisher=Publisher(),
            review_queue=Publisher(),
        ).execute()
        states = {name: await run_state(factory, run_id) for name, run_id in runs.items()}
        await engine.dispose()
        return {**states, **{f"id:{k}": (v,) for k, v in runs.items()}}

    result = asyncio.run(scenario())

    assert result["t12"] == ("queued", 1, None)
    assert result["t13"] == ("failed", 3, "lease_expired")
    assert result["t17"] == ("publishing", 1, None)
    assert result["retry"][0] == "queued" and result["live"][0] == "running"
    ids = {name: result[f"id:{name}"][0] for name in ("t12", "t13", "t17", "t18")}
    assert sorted(published, key=str) == sorted(
        [("run", ids["t12"]), ("run", ids["t13"]), ("publish", ids["t17"]), ("run", ids["t18"])],
        key=str,
    )


@pytest.mark.integration
def test_run_update_notify_is_delivered_on_commit_and_not_on_rollback(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> tuple[UUID, list[dict[str, str]]]:
        run_id = await insert_run(
            factory, env, state="running", attempt=1, lease=datetime.now(UTC) - timedelta(minutes=1)
        )
        async with listen(env) as events:
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                assert await uow.runs.finish(
                    run_id,
                    from_state="running",
                    worker_id=None,
                    state="failed",
                    error_code="lease_expired",
                    error_message=None,
                    now=datetime.now(UTC),
                )
                await uow.rollback()
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                await uow.session.execute(text("UPDATE runs SET worker_id = 'w'"))
                await uow.runs.finish(
                    run_id,
                    from_state="running",
                    worker_id="w",
                    state="cancelled",
                    error_code="cancelled_by_user",
                    error_message=None,
                    now=datetime.now(UTC),
                )
                await uow.commit()
            await asyncio.sleep(0.3)
        await engine.dispose()
        return run_id, events

    run_id, events = asyncio.run(scenario())
    assert [event["status"] for event in events if event["run_id"] == str(run_id)] == ["cancelled"]


@pytest.mark.integration
def test_lazy_publisher_connects_on_first_use_and_retries_after_an_outage(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> dict[str, Any]:
        run_id = await insert_run(factory, env)
        async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
            message = await uow.runs.run_message(run_id)
        assert message is not None
        down = LazyAmqpPublisher("amqp://app:app@127.0.0.1:1/")
        with pytest.raises(OSError):
            await down.publish_confirmed(message)
        await down.aclose()
        publisher = LazyAmqpPublisher(env.rabbitmq_url, SHORT_DELAYS)
        await publisher.publish_confirmed(message)
        await publisher.aclose()
        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            queue = await channels.consumer_queue(run_queue("fast"))
            received = await queue.get(no_ack=True)
            assert received is not None
            body: dict[str, Any] = json.loads(received.body)
        await engine.dispose()
        return body

    assert asyncio.run(scenario())["schema"] == "review.run/v1"


@pytest.mark.integration
def test_push_during_the_github_post_ends_the_run_cancelled_not_succeeded(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def push() -> None:
        from app.modules.reviews.infrastructure.github_pull_request_projection import (
            SqlAlchemyPullRequestProjectionUnitOfWork,
        )

        async with SqlAlchemyPullRequestProjectionUnitOfWork(factory) as uow:
            await uow.session.execute(
                text("UPDATE code_changes SET head_sha = :sha"), {"sha": "d" * 40}
            )
            await uow.runs.cancel_for_pr(env.pr_id, "superseded", datetime.now(UTC))
            await uow.commit()

    github = GitHub(during_post=push)
    model = Model(factory)

    async def scenario() -> tuple[tuple[Any, ...], list[tuple[Any, ...]]]:
        run_id = await insert_run(factory, env)
        async with (
            amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels,
            running_worker(env, factory, model, github, channels),
        ):
            await publish_run(channels, factory, run_id)
            state = await wait_for_state(factory, run_id, "cancelled", "succeeded")
            await wait_for_check_run(github, "completed")
        async with factory() as session:
            findings = [
                tuple(row) for row in await session.execute(text("SELECT published FROM findings"))
            ]
        await engine.dispose()
        return state, findings

    state, findings = asyncio.run(scenario())

    assert state == ("cancelled", 1, "superseded")
    # The review reached GitHub before the push was seen, so its findings are published.
    assert findings == [(True,)]
    assert [(status, conclusion) for _, status, conclusion in github.check_runs][-1] == (
        "completed",
        "cancelled",
    )


@pytest.mark.integration
def test_publishing_is_persisted_in_runs_while_github_receives_the_review(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    states_during_post: list[str] = []

    async def read_state() -> None:
        async with factory() as session:
            states_during_post.append(str(await session.scalar(text("SELECT state FROM runs"))))

    github = GitHub(during_post=read_state)
    model = Model(factory)

    async def scenario() -> tuple[Any, ...]:
        run_id = await insert_run(factory, env)
        async with (
            amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels,
            running_worker(env, factory, model, github, channels),
        ):
            await publish_run(channels, factory, run_id)
            state = await wait_for_state(factory, run_id, "succeeded")
            await wait_for_check_run(github, "completed")
        await engine.dispose()
        return state

    state = asyncio.run(scenario())

    assert model.observed[0][0] == "running"
    assert states_during_post == ["publishing"]
    assert state == ("succeeded", 1, None)


_APP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_APP_PEM = _APP_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
_FILE_SHA = "f" * 40
_FILE_TEXT = b"line10\nline11\nline12\n"


class FakeGitHubApi:
    """GitHub REST as the worker uses it, on a MockTransport (no network)."""

    def __init__(self, *, agents_md: bytes | None = None, commits: tuple[str, ...] = ()) -> None:
        self.agents_md = agents_md
        assert len(commits) < 10, "the handler ignores page: a full page would repeat as page 2"
        self.commits = commits
        self.reviews: list[dict[str, Any]] = []
        self.requests: list[str] = []

    def blob(self, sha: str, data: bytes) -> httpx.Response:
        content = base64.b64encode(data).decode()
        return httpx.Response(
            200, json={"sha": sha, "encoding": "base64", "content": content, "size": len(data)}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append(f"{method} {path}")
        repo = "/repos/octo/repo"
        if path == "/app/installations/17/access_tokens":
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"{repo}/pulls/7":
            ref = {"ref": "feature", "sha": HEAD, "repo": {"full_name": "octo/repo"}}
            base = {"ref": "main", "sha": BASE, "repo": {"full_name": "octo/repo"}}
            return httpx.Response(
                200,
                json={
                    "id": 907,
                    "number": 7,
                    "title": "PR",
                    "body": None,
                    "user": {"login": "octocat"},
                    "head": ref,
                    "base": base,
                    "labels": [{"name": "ai-review"}],
                    "draft": False,
                    "changed_files": 1,
                    "additions": 1,
                    "deletions": 0,
                },
            )
        if path == f"{repo}/pulls/7/commits":
            return httpx.Response(200, json=[{"commit": {"message": m}} for m in self.commits])
        if path == f"{repo}/pulls/7/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "app/example.py",
                        "status": "modified",
                        "sha": _FILE_SHA,
                        "additions": 1,
                        "deletions": 0,
                        "changes": 1,
                        "patch": PATCH,
                    }
                ],
            )
        if path == f"{repo}/git/blobs/{_FILE_SHA}":
            return self.blob(_FILE_SHA, _FILE_TEXT)
        if path == f"{repo}/git/blobs/{'a' * 40}" and self.agents_md is not None:
            return self.blob("a" * 40, self.agents_md)
        if path == f"{repo}/compare/{BASE}...{HEAD}":
            return httpx.Response(200, json={"merge_base_commit": {"sha": BASE}})
        if path == f"{repo}/git/trees/{BASE}":
            tree = [{"path": "README.md", "type": "blob", "sha": "c" * 40, "size": 3}]
            if self.agents_md is not None:
                tree.append({"path": "AGENTS.md", "type": "blob", "sha": "a" * 40, "size": 9})
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if path == f"{repo}/git/trees/{HEAD}":
            tree = [{"path": "app/example.py", "type": "blob", "sha": _FILE_SHA, "size": 21}]
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if path == f"{repo}/commits/{HEAD}/check-runs":
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if path.startswith(f"{repo}/check-runs"):
            return httpx.Response(201 if method == "POST" else 200, json={"id": 31})
        if path == f"{repo}/pulls/7/reviews" and method == "GET":
            return httpx.Response(200, json=[])
        if path == f"{repo}/pulls/7/reviews":
            self.reviews.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 501})
        if path == f"{repo}/pulls/7/reviews/501/comments":
            return httpx.Response(200, json=[{"id": 9001}])
        raise AssertionError(f"unexpected GitHub call {method} {request.url}")


class FakeLlm:
    """An OpenAI-compatible chat endpoint answering the conventions and review tasks."""

    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        task = body["response_format"]["json_schema"]["name"]
        self.tasks.append(task)
        self.bodies.append(body)
        answer: dict[str, object] = (
            {
                "files": [{"path": "app/example.py", "relevance": "Changed module."}],
                "key_patterns": ["Pattern one.", "Pattern two.", "Pattern three."],
                "recommendations": [
                    f"Recommendation {n} (from: standard/correctness)"
                    for n in ("one", "two", "three", "four", "five")
                ],
            }
            if task == "RepoConventionsDraft"
            else review_output()
        )
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
            },
        )


LLM_ENV = {
    "LLM_MODEL": "test-model",
    "LLM_BASE_URL": "https://llm.test/v1",
    "LLM_API_KEYS": "test-key",
    "LLM_CONTEXT_WINDOW": "100000",
    "LLM_MAX_OUTPUT_TOKENS": "8000",
    "LLM_PRICE_INPUT_PER_MTOK": "1",
    "LLM_PRICE_OUTPUT_PER_MTOK": "2",
}


def _main_settings(env: Env, extra: Mapping[str, str]) -> WorkerSettings:
    assert env.rabbitmq_url is not None
    return WorkerSettings.from_environment(
        {
            "DATABASE_URL": f"{env.database_url}?options=-csearch_path%3D{env.schema}",
            "RABBITMQ_URL": env.rabbitmq_url,
            "WORKER_ID": "main-composition",
            "GITHUB_APP_ID": "1",
            "GITHUB_APP_PRIVATE_KEY": _APP_PEM,
            "GITHUB_API_URL": "https://api.github.test",
            **extra,
        }
    )


async def _run_main_worker(
    env: Env, settings: WorkerSettings, github: FakeGitHubApi, llm: FakeLlm, *states: str
) -> tuple[UUID, tuple[Any, ...], list[str]]:
    """Run ``run_worker`` on one Run; the statuses are its state at insert, then each NOTIFY."""
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await insert_run(factory, env)
    async with factory() as session:
        statuses = [
            str(await session.scalar(text("SELECT state FROM runs WHERE id = :id"), {"id": run_id}))
        ]
    worker = asyncio.create_task(
        run_worker(
            settings,
            delays=SHORT_DELAYS,
            github_transport=httpx.MockTransport(github),
            llm_transport=httpx.MockTransport(llm),
        )
    )
    try:
        async with listen(env) as events:
            async with amqp_channels(env.rabbitmq_url or "", SHORT_DELAYS) as channels:
                await publish_run(channels, factory, run_id)
            state = await wait_for_state(factory, run_id, *states)
            # The check-run and the ack follow the last commit.
            await asyncio.sleep(0.5)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await engine.dispose()
    statuses += [event["status"] for event in events if event["run_id"] == str(run_id)]
    return run_id, state, statuses


@pytest.mark.integration
@pytest.mark.parametrize("agents_md", [None, b"# Rules\n"], ids=["no-agents-md", "agents-md"])
def test_main_composition_reviews_through_the_llm_gateway(
    env: Env, agents_md: bytes | None
) -> None:
    github, llm = FakeGitHubApi(agents_md=agents_md), FakeLlm()
    settings = _main_settings(env, LLM_ENV)
    run_id, state, statuses = asyncio.run(
        _run_main_worker(env, settings, github, llm, "succeeded", "failed")
    )
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def read() -> tuple[list[tuple[Any, ...]], int]:
        actions = await tools(factory, run_id)
        async with factory() as session:
            usage = await session.scalar(
                text("SELECT count(*) FROM usage_events WHERE run_id = :id"), {"id": run_id}
            )
        await engine.dispose()
        return actions, int(usage or 0)

    actions, usage_rows = asyncio.run(read())
    names = [action[0] for action in actions]

    assert state == ("succeeded", 1, None)
    # "queued" is the state insert_run wrote; the three transitions come from NOTIFY.
    assert statuses == ["queued", "running", "publishing", "succeeded"]
    assert llm.tasks[-1] != "RepoConventionsDraft" and "RepoConventionsDraft" in llm.tasks
    assert names.count("llm.review_output") == 1
    assert names.count("llm.call") >= 2
    assert usage_rows >= 2
    assert github.reviews and github.reviews[0]["commit_id"] == HEAD


@pytest.mark.integration
def test_main_composition_sends_escaped_commit_messages_to_the_review_llm_call(
    env: Env,
) -> None:
    commits = ("fix: <escape> & keep", "feat: a > b && c < d")
    github, llm = FakeGitHubApi(commits=commits), FakeLlm()
    settings = _main_settings(env, LLM_ENV)
    _, state, _ = asyncio.run(_run_main_worker(env, settings, github, llm, "succeeded", "failed"))

    assert state == ("succeeded", 1, None)
    assert "GET /repos/octo/repo/pulls/7/commits" in github.requests
    reviews = [
        body
        for body in llm.bodies
        if body["response_format"]["json_schema"]["name"] == "ReviewOutput"
    ]
    assert len(reviews) == 1
    users = [m["content"] for m in reviews[0]["messages"] if m["role"] == "user"]
    assert len(users) == 1
    assert (
        "<commit_messages>\n"
        "<commit>fix: &lt;escape&gt; &amp; keep</commit>\n"
        "<commit>feat: a &gt; b &amp;&amp; c &lt; d</commit>\n"
        "</commit_messages>"
    ) in users[0]
    for message in commits:
        assert message not in users[0]


@pytest.mark.integration
def test_main_composition_without_llm_config_names_the_missing_configuration(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    github, llm = FakeGitHubApi(), FakeLlm()
    settings = _main_settings(env, {})
    with caplog.at_level(logging.WARNING):
        run_id, state, _ = asyncio.run(_run_main_worker(env, settings, github, llm, "failed"))
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def read() -> tuple[str, list[tuple[Any, ...]]]:
        async with factory() as session:
            message = await session.scalar(
                text("SELECT error_message FROM runs WHERE id = :id"), {"id": run_id}
            )
        actions = await tools(factory, run_id)
        await engine.dispose()
        return str(message), actions

    message, actions = asyncio.run(read())
    fetches = [action for action in actions if action[0] == "vcs.fetch_diff"]

    assert settings.llm is None
    assert "LLM_MODEL is not set" in caplog.text
    assert state == ("failed", 3, "llm_unavailable")
    assert "not configured" in message
    assert llm.tasks == []
    # The diff step succeeds: the missing LLM never masks a VCS step.
    assert fetches and all("error" not in (action[2] or {}) for action in fetches)


@pytest.mark.integration
def test_repeated_delivery_failures_route_to_dlq_across_reconnects(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS",
        0.0,
    )

    run_id = uuid4()
    body = {
        "schema": "review.run/v1",
        "message_id": str(run_id),
        "run_id": str(run_id),
        "workspace_id": str(uuid4()),
        "installation_id": 17,
        "repo": {
            "id": str(uuid4()),
            "provider": "github",
            "external_id": 101,
            "full_name": "octo/repo",
        },
        "pr": {
            "number": 7,
            "head_sha": HEAD,
            "base_sha": BASE,
            "base_ref": "main",
        },
        "engine": "fast",
        "rule_version_id": str(env.rule_id),
        "prompt_version_id": str(env.prompt_id),
        "trigger": "webhook",
        "attempt": 1,
        "requested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    async def publish_initial() -> None:
        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            connection = channels.connection
            channel = await connection.channel()
            exchange = await channel.get_exchange(EXCHANGE, ensure=False)
            await exchange.publish(
                aio_pika.Message(
                    json.dumps(body).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=str(run_id),
                ),
                routing_key=run_queue("fast"),
            )

    asyncio.run(publish_initial())

    deliveries_seen = 0

    async def deliver_once_and_fail() -> None:
        nonlocal deliveries_seen
        from app.modules.reviews.infrastructure.amqp import _unexpected_delivery_attempts

        _unexpected_delivery_attempts.clear()

        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            queue = await channels.consumer_queue(run_queue("fast"))
            async with queue.iterator() as messages:

                async def _consume() -> None:
                    nonlocal deliveries_seen
                    async for message in messages:
                        deliveries_seen += 1

                        async def failing_handler(_: RunDeliveryPointer) -> DeliveryOutcome:
                            raise RuntimeError("handler failure")

                        await handle_run_delivery(
                            message,
                            handler=failing_handler,
                            publisher=channels.publisher,
                        )
                        break

                await asyncio.wait_for(_consume(), timeout=10.0)

    for _ in range(3):
        asyncio.run(deliver_once_and_fail())

    assert deliveries_seen == 3

    async def verify_dlq() -> tuple[int, dict[str, Any] | None]:
        async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
            probe = await channels.connection.channel()
            fast_q = await probe.declare_queue(run_queue("fast"), passive=True)
            fast_count = fast_q.declaration_result.message_count
            dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)

            dlq_msg: aio_pika.abc.AbstractIncomingMessage | None = None
            async with dlq.iterator() as messages:

                async def _consume_dlq() -> None:
                    nonlocal dlq_msg
                    async for msg in messages:
                        await msg.ack()
                        dlq_msg = msg
                        break

                await asyncio.wait_for(_consume_dlq(), timeout=10.0)
            dlq_headers = dict(dlq_msg.headers or {}) if dlq_msg is not None else None
            return int(fast_count or 0), dlq_headers

    fast_count, dlq_headers = asyncio.run(verify_dlq())
    assert fast_count == 0
    assert dlq_headers is not None
    assert dlq_headers.get("x-attempt") == 2


@pytest.mark.integration
@pytest.mark.parametrize("failed_route", [run_queue("fast"), PUBLISH_QUEUE])
def test_main_worker_survives_real_closed_channel_ack_and_processes_next_run(
    env: Env, monkeypatch: pytest.MonkeyPatch, failed_route: str
) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        github, llm = FakeGitHubApi(), FakeLlm()
        failed, stop = asyncio.Event(), asyncio.Event()
        errors: list[ChannelInvalidStateError] = []
        first = await insert_run(factory, env)
        broken = False
        original_ack = aio_pika.IncomingMessage.ack

        async def break_first_ack(
            message: aio_pika.IncomingMessage, multiple: bool = False
        ) -> None:
            nonlocal broken
            if (
                not broken
                and message.routing_key == failed_route
                and json.loads(message.body)["run_id"] == str(first)
            ):
                broken = True
                # Close the underlying transport, allowing RobustChannel to restore.
                # Closing RobustChannel itself would intentionally disable that recovery.
                await message.channel.close(exc=RuntimeError("controlled delivery transport loss"))
                try:
                    await original_ack(message, multiple=multiple)
                except ChannelInvalidStateError as error:
                    errors.append(error)
                    failed.set()
                    raise
                pytest.fail("ACK on the closed delivery channel unexpectedly succeeded")
            await original_ack(message, multiple=multiple)

        async def github_response(request: httpx.Request) -> httpx.Response:
            if (
                failed_route == run_queue("fast")
                and request.method == "POST"
                and request.url.path.endswith("/pulls/7/reviews")
            ):
                # Publication must progress after its sibling's actual ACK failure.
                await failed.wait()
            return github(request)

        monkeypatch.setattr(aio_pika.IncomingMessage, "ack", break_first_ack)
        task = asyncio.create_task(
            run_worker(
                _main_settings(env, LLM_ENV),
                stop=stop,
                delays=SHORT_DELAYS,
                github_transport=httpx.MockTransport(github_response),
                llm_transport=httpx.MockTransport(llm),
            )
        )
        try:
            async with asyncio.timeout(20):
                async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
                    await publish_run(channels, factory, first)
                    await failed.wait()
                    assert not task.done()
                    assert await wait_for_state(factory, first, "succeeded") == (
                        "succeeded",
                        1,
                        None,
                    )
                    # One active Run per PR; same-head rerun avoids webhook uniqueness.
                    second = await insert_run(factory, env, trigger="rerun")
                    await publish_run(channels, factory, second)
                    assert await wait_for_state(factory, second, "succeeded", timeout=10) == (
                        "succeeded",
                        1,
                        None,
                    )
                    observer = await channels.connection.channel()
                    restored = await observer.declare_queue(failed_route, passive=True)
                    assert restored.declaration_result.consumer_count == 1
                    assert not task.done()
                    assert len(errors) == 1 and len(github.reviews) == 2
                stop.set()
                await task
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()

    asyncio.run(scenario())


@pytest.fixture
def guarded_queue_database(
    guard_database: tuple[str, str, UUID],
) -> Iterator[tuple[tuple[str, str, UUID], str]]:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if url is None:
        pytest.skip("set TEST_RABBITMQ_URL for persisted-pointer consumer tests")
    asyncio.run(_reset_topology(url))
    try:
        yield guard_database, url
    finally:
        asyncio.run(_reset_topology(url))


@pytest.mark.integration
@pytest.mark.parametrize("forged_claim", ["provider", "workspace", "project", "head"])
def test_actual_worker_rejects_forged_gitlab_pointer_before_claim(
    guarded_queue_database: tuple[tuple[str, str, UUID], str],
    forged_claim: str,
) -> None:
    database, rabbit = guarded_queue_database
    database_url, schema, _ = database

    async def scenario() -> None:
        async with asyncio.timeout(45), rig(database) as fixture:
            pointer, origin, _ = await seeded(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET message_published_at=now(), available_at=now() "
                        "WHERE id=:id"
                    ),
                    {"id": pointer.run_id},
                )
            body: dict[str, Any] = {
                "schema": "review.run/v1",
                "message_id": str(pointer.run_id),
                "run_id": str(pointer.run_id),
                "workspace_id": str(fixture.workspace),
                "installation_id": 71,
                "repo": {
                    "id": str(pointer.repository_id),
                    "provider": "gitlab",
                    "external_id": 17,
                    "full_name": "team/project",
                },
                "pr": {
                    "number": 12,
                    "head_sha": "a" * 40,
                    "base_sha": "b" * 40,
                    "base_ref": "main",
                },
                "engine": "fast",
                "rule_version_id": str(pointer.rule_version_id),
                "prompt_version_id": str(pointer.prompt_version_id),
                "trigger": "rerun",
                "attempt": 1,
                "requested_at": "2026-10-19T12:00:00Z",
            }
            if forged_claim == "provider":
                body["repo"]["provider"] = "github"
            elif forged_claim == "workspace":
                body["workspace_id"] = "20000000-0000-0000-0000-000000000001"
            elif forged_claim == "project":
                body["repo"]["external_id"] = 18
            else:
                body["pr"]["head_sha"] = "c" * 40
            settings = WorkerSettings.from_environment(
                {
                    "DATABASE_URL": f"{database_url}?options=-csearch_path%3D{schema}",
                    "RABBITMQ_URL": rabbit,
                    "WORKER_ID": "pointer-guard-proof",
                }
            )
            provider_calls: list[UUID] = []

            def provider(claimed: ClaimedAttempt) -> Model:
                provider_calls.append(claimed.run_id)
                raise AssertionError("forged pointer must not construct a provider")

            worker = asyncio.create_task(
                run_worker(settings, delays=SHORT_DELAYS, provider_factory=provider)
            )
            try:
                async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                    probe = await channels.connection.channel(publisher_confirms=True)
                    try:
                        async with asyncio.timeout(20):
                            while True:
                                if worker.done():
                                    await worker
                                ready = await probe.declare_queue(run_queue("fast"), passive=True)
                                # RobustChannel caches passive declarations; refresh broker counts.
                                await ready.declare()
                                if ready.declaration_result.consumer_count:
                                    break
                                await asyncio.sleep(0.05)
                        exchange = await probe.get_exchange(EXCHANGE, ensure=False)
                        await exchange.publish(
                            aio_pika.Message(
                                json.dumps(body).encode(),
                                message_id=str(pointer.run_id),
                                content_type="application/json",
                                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            ),
                            routing_key=run_queue("fast"),
                        )
                        dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)
                        dead: dict[str, Any] | None = None
                        async with asyncio.timeout(10):
                            while True:
                                if worker.done():
                                    await worker
                                state = await run_state(fixture.sessions, pointer.run_id)
                                message = await dlq.get(fail=False)
                                if message is not None:
                                    dead = json.loads(message.body)
                                    # A miswired UUID handler would retry before dead-lettering.
                                    assert "x-attempt" not in (message.headers or {})
                                    deaths = (message.headers or {})["x-death"]
                                    assert isinstance(deaths, list) and len(deaths) == 1
                                    death = deaths[0]
                                    assert isinstance(death, dict)
                                    assert death["reason"] == "rejected"
                                    assert death["queue"] == "review.run.fast"
                                    await message.ack()
                                if state != ("queued", 0, None) or dead is not None:
                                    break
                                await asyncio.sleep(0.05)
                        # This assertion measures committed mutation in the pre-wiring RED.
                        assert state == ("queued", 0, None)
                        assert dead == body
                        assert provider_calls == []
                        async with fixture.sessions() as session:
                            assert (
                                await session.scalar(
                                    text(
                                        "SELECT origin_provider_installation_id FROM runs "
                                        "WHERE id=:id"
                                    ),
                                    {"id": pointer.run_id},
                                )
                                == origin
                            )
                    finally:
                        await probe.close()
            finally:
                worker.cancel()
                results = await asyncio.wait_for(
                    asyncio.gather(worker, return_exceptions=True),
                    timeout=5,
                )
                assert isinstance(results[0], asyncio.CancelledError)

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("active", [False, True], ids=["idle", "review-and-index-active"])
def test_main_worker_stop_releases_persisted_leases_before_connection_close_and_recovers(
    env: Env, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    import app.bootstrap.worker_shutdown as shutdown

    monkeypatch.setattr(shutdown, "_DRAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(shutdown, "_CLEANUP_TIMEOUT", 2.0)

    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        github, llm = FakeGitHubApi(), FakeLlm()
        stop, review_entered, index_entered = (asyncio.Event() for _ in range(3))
        provider_cancelled: set[str] = set()
        before_close: list[tuple[Any, ...]] = []
        run_id = await insert_run(factory, env) if active else None
        async with factory() as session:
            repository_id = await session.scalar(
                text("SELECT repository_id FROM code_changes WHERE id=:id"), {"id": env.pr_id}
            )
        assert isinstance(repository_id, UUID)
        if active:
            async with factory.begin() as session:
                await session.execute(
                    text("INSERT INTO code_index_states(repository_id,generation) VALUES (:id,1)"),
                    {"id": repository_id},
                )

        async def github_response(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/octo/repo":
                index_entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    provider_cancelled.add("index")
                    raise
            return github(request)

        async def llm_response(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["response_format"]["json_schema"]["name"] != "RepoConventionsDraft":
                review_entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    provider_cancelled.add("review")
                    raise
            return llm(request)

        original_channels = amqp_channels

        @contextlib.asynccontextmanager
        async def observed_channels(url: str, delays: RetryDelays) -> AsyncIterator[AmqpChannels]:
            async with original_channels(url, delays) as channels:
                try:
                    yield channels
                finally:
                    if not before_close:
                        assert not channels.connection.is_closed
                        async with factory() as session:
                            if active:
                                row = (
                                    await session.execute(
                                        text(
                                            "SELECT state,attempt,worker_id,lease_until,error_code "
                                            "FROM runs WHERE id=:id"
                                        ),
                                        {"id": run_id},
                                    )
                                ).one()
                                index = (
                                    await session.execute(
                                        text(
                                            "SELECT lease_token,lease_expires_at,pending_bytes "
                                            "FROM code_index_states WHERE repository_id=:id"
                                        ),
                                        {"id": repository_id},
                                    )
                                ).one()
                                assert tuple(row[:3]) == ("running", 1, "main-composition")
                                assert (
                                    row.lease_until < datetime.now(UTC) and row.error_code is None
                                )
                                assert tuple(index) == (None, None, 0)
                                before_close.append(tuple(row))
                            else:
                                assert (
                                    await session.scalar(
                                        text(
                                            "SELECT count(*) FROM runs "
                                            "WHERE lease_until IS NOT NULL"
                                        )
                                    )
                                    == 0
                                )
                                before_close.append(("idle",))

        monkeypatch.setattr(worker_module, "amqp_channels", observed_channels)
        task: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(25):
                async with amqp_channels(env.rabbitmq_url, SHORT_DELAYS) as channels:
                    publishing = await channels.connection.channel()
                    await index_amqp.declare_topology(publishing, SHORT_DELAYS)
                    wakeups = await index_amqp.AmqpIndexWakeups.open(publishing)
                    task = asyncio.create_task(
                        run_worker(
                            _main_settings(env, LLM_ENV),
                            stop=stop,
                            delays=SHORT_DELAYS,
                            github_transport=httpx.MockTransport(github_response),
                            llm_transport=httpx.MockTransport(llm_response),
                        )
                    )
                    # Observe real broker subscriptions before triggering work or idle stop.
                    readiness_started = asyncio.get_running_loop().time()
                    while True:
                        attached = []
                        for route in (run_queue("fast"), PUBLISH_QUEUE, index_amqp.QUEUE):
                            queue = await publishing.declare_queue(route, passive=True)
                            result = await queue.declare()
                            attached.append(result.consumer_count)
                        if attached == [1, 1, 1]:
                            break
                        assert asyncio.get_running_loop().time() - readiness_started < 5, attached
                        assert not task.done()
                        await asyncio.sleep(0.01)
                    if active:
                        assert run_id is not None
                        await publish_run(channels, factory, run_id)
                        await wakeups.wake(repository_id, 1)
                        await review_entered.wait()
                        await index_entered.wait()
                        async with factory() as session:
                            lease = await session.scalar(
                                text("SELECT lease_until FROM runs WHERE id=:id"), {"id": run_id}
                            )
                            index_lease = (
                                await session.execute(
                                    text(
                                        "SELECT lease_token,lease_expires_at "
                                        "FROM code_index_states "
                                        "WHERE repository_id=:id"
                                    ),
                                    {"id": repository_id},
                                )
                            ).one()
                        assert lease > datetime.now(UTC)
                        assert index_lease.lease_token is not None
                        assert index_lease.lease_expires_at > datetime.now(UTC)
                        # QoS1 keeps these unstarted duplicate pointers broker-ready.
                        await publish_run(channels, factory, run_id)
                        await wakeups.wake(repository_id, 1)
                    stop.set()
                    await task
                    assert len(before_close) == 1
                    if active:
                        assert run_id is not None
                        assert provider_cancelled == {"review", "index"}
                        for route in (run_queue("fast"), index_amqp.QUEUE):
                            queue = await publishing.declare_queue(route, passive=True)
                            result = await queue.declare()
                            assert result.consumer_count == 0
                            assert (result.message_count or 0) >= 2
                            pointers = []
                            while (message := await queue.get(fail=False)) is not None:
                                pointers.append(json.loads(message.body))
                                await message.ack()
                            assert len(pointers) >= 2
                            key, expected = (
                                ("run_id", run_id)
                                if route == run_queue("fast")
                                else ("repository_id", repository_id)
                            )
                            assert all(pointer[key] == str(expected) for pointer in pointers)
                        # Use the existing reconciler/publisher and a healthy real composition.
                        assert (
                            await ReconcileRuns(
                                uow_factory=partial(SqlAlchemyRunLifecycleUnitOfWork, factory),
                                run_publisher=channels.publisher,
                                review_queue=channels.publisher,
                            ).execute()
                            == 1
                        )
                        assert await run_state(factory, run_id) == ("queued", 1, None)
                        recovered_stop = asyncio.Event()
                        recovery = asyncio.create_task(
                            run_worker(
                                _main_settings(env, LLM_ENV),
                                stop=recovered_stop,
                                delays=SHORT_DELAYS,
                                github_transport=httpx.MockTransport(github),
                                llm_transport=httpx.MockTransport(llm),
                            )
                        )
                        try:
                            assert await wait_for_state(factory, run_id, "succeeded") == (
                                "succeeded",
                                2,
                                None,
                            )
                        finally:
                            recovered_stop.set()
                            await recovery
                    else:
                        assert provider_cancelled == set() and llm.tasks == []
        finally:
            stop.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("disposition", ["valid", "missing", "retired"])
def test_real_consumer_acks_valid_missing_and_retired_pointers(
    guarded_queue_database: tuple[tuple[str, str, UUID], str],
    provider: str,
    disposition: str,
) -> None:
    database, rabbit = guarded_queue_database

    async def scenario() -> None:
        async with asyncio.timeout(30), rig(database) as fixture:
            pointer, origin, _ = await seeded(fixture, provider)
            run_id = pointer.run_id
            if disposition == "missing":
                run_id = UUID("20000000-0000-0000-0000-000000000002")
            elif disposition == "retired":
                if provider == "gitlab":
                    await fixture.lifecycle.revoke_installation(installation_id=origin)
                else:
                    async with fixture.sessions.begin() as session:
                        await session.execute(
                            text("UPDATE provider_installations SET deleted_at=now() WHERE id=:id"),
                            {"id": origin},
                        )
            pending = StoredRunMessage(
                run_id=run_id,
                workspace_id=fixture.workspace,
                installation_id=71,
                repository_id=pointer.repository_id,
                repository_external_id=17,
                repository_full_name="renamed/project",
                pr_number=12,
                head_sha="a" * 40,
                base_sha="b" * 40,
                base_ref="old-branch",
                engine="fast",
                rule_version_id=pointer.rule_version_id,
                prompt_version_id=pointer.prompt_version_id,
                attempt=99,
                requested_at=datetime(2000, 1, 1, tzinfo=UTC),
                provider=provider,
                trigger="rerun",
            )
            calls: list[UUID] = []

            async def lifecycle(accepted: UUID) -> DeliveryOutcome:
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                calls.append(accepted)
                return DeliveryOutcome.ACK

            handler = HandleRunDelivery(SqlAlchemyRunDeliveryReader(fixture.sessions), lifecycle)
            async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                await channels.publisher.publish_confirmed(pending)
                queue = await channels.consumer_queue(run_queue("fast"))
                try:
                    async with asyncio.timeout(10):
                        while (message := await queue.get(fail=False)) is None:
                            await asyncio.sleep(0.05)
                    await handle_run_delivery(message, handler.execute, channels.publisher)
                    assert message.processed
                    assert calls == ([pointer.run_id] if disposition == "valid" else [])
                    dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)
                    try:
                        assert await dlq.get(fail=False) is None
                    finally:
                        await dlq.channel.close()
                finally:
                    await queue.channel.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_unavailable_sql_reader_retries_twice_then_real_broker_dead_letters(
    guarded_queue_database: tuple[tuple[str, str, UUID], str],
) -> None:
    database, rabbit = guarded_queue_database

    async def scenario() -> None:
        async with asyncio.timeout(30), rig(database) as fixture:
            pointer, _, _ = await seeded(fixture)
            pending = StoredRunMessage(
                run_id=pointer.run_id,
                workspace_id=fixture.workspace,
                installation_id=71,
                repository_id=pointer.repository_id,
                repository_external_id=17,
                repository_full_name="team/project",
                pr_number=12,
                head_sha="a" * 40,
                base_sha="b" * 40,
                base_ref="main",
                engine="fast",
                rule_version_id=pointer.rule_version_id,
                prompt_version_id=pointer.prompt_version_id,
                attempt=1,
                requested_at=datetime(2026, 10, 19, tzinfo=UTC),
                provider="gitlab",
                trigger="rerun",
            )
            calls: list[UUID] = []

            async def lifecycle(run_id: UUID) -> DeliveryOutcome:
                calls.append(run_id)
                return DeliveryOutcome.ACK

            # Bound but not listening: real driver refusal, with no external service.
            with socket.socket() as unavailable:
                unavailable.bind(("127.0.0.1", 0))
                url = make_url(database[0]).set(port=unavailable.getsockname()[1])
                engine = create_async_engine(url, pool_size=1, max_overflow=0)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                handler = HandleRunDelivery(SqlAlchemyRunDeliveryReader(sessions), lifecycle)
                try:
                    async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                        await channels.publisher.publish_confirmed(pending)
                        queue = await channels.consumer_queue(run_queue("fast"))
                        dlq = await channels.consumer_queue(DEAD_LETTER_QUEUE)
                        try:
                            attempts: list[object] = []
                            for _ in range(3):
                                async with asyncio.timeout(10):
                                    while (message := await queue.get(fail=False)) is None:
                                        await asyncio.sleep(0.05)
                                attempts.append((message.headers or {}).get("x-attempt", 0))
                                await handle_run_delivery(
                                    message, handler.execute, channels.publisher
                                )
                                assert message.processed
                            assert attempts == [0, 1, 2]
                            assert calls == []
                            async with asyncio.timeout(10):
                                while (dead := await dlq.get(fail=False)) is None:
                                    await asyncio.sleep(0.05)
                            assert (dead.headers or {}).get("x-attempt") == 2
                            assert json.loads(dead.body)["run_id"] == str(pointer.run_id)
                            await dead.ack()
                            assert await queue.get(fail=False) is None
                            assert await run_state(fixture.sessions, pointer.run_id) == (
                                "queued",
                                0,
                                None,
                            )
                        finally:
                            await queue.channel.close()
                            await dlq.channel.close()
                finally:
                    await engine.dispose()

    asyncio.run(scenario())
