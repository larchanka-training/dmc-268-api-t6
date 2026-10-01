"""Worker against real RabbitMQ and PostgreSQL with fake GitHub and ReviewModel (#34).

Opt-in: set ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL``
(disposable RabbitMQ: the tests delete and redeclare the review topology).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import psycopg
import pytest
from aiormq.exceptions import ChannelPreconditionFailed
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.reviews_api import get_run_repository
from app.common.infrastructure.db.leader import run_as_leader
from app.main import app
from app.modules.reviews.application.check_runs import CheckRunTarget, CheckRunView
from app.modules.reviews.application.conventions import (
    ConventionsRequest,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.determine_ci_eligibility import CiEligibility
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
from app.modules.reviews.application.publish_run_review import ReviewSubmission, SubmittedReview
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.review_output import PublishedFinding
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
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import GitHubAdapters, WorkerSettings, compose_worker_process, run_worker
from tests.portal_test_client import authenticated_test_client

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
    database_url: str
    schema: str
    rabbitmq_url: str
    pr_id: UUID
    rule_id: UUID
    prompt_id: UUID

    def engine(self) -> AsyncEngine:
        return create_async_engine(
            self.database_url,
            connect_args={"options": f"-csearch_path={self.schema}"},
            poolclass=NullPool,
        )


async def _reset_topology(url: str) -> None:
    connection = await aio_pika.connect(url)
    try:
        channel = await connection.channel()
        names = [DEAD_LETTER_QUEUE, PUBLISH_QUEUE]
        for engine in ENGINES:
            names.append(run_queue(engine))
            names.extend(retry_queue(key, engine) for key in RETRY_KEYS)
        for name in names:
            await channel.queue_delete(name)
        for exchange in (EXCHANGE, RETRY_EXCHANGE, DEAD_LETTER_EXCHANGE):
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
            yield Env(database_url, schema, rabbitmq_url, pr_id, rule_id, prompt_id)
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
                "available_at, attempt, lease_until) VALUES (:id, :pr, :base, 'main', :head, "
                ":state, :trigger, :key, 'fast', :rule, :prompt, :available, :attempt, :lease)"
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
                            "current_database() AND state LIKE 'idle in transaction%'"
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
    async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
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

    async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
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

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
        return b"line10\nline11\nline12\n"


@dataclass
class GitHub:
    check_runs: list[tuple[UUID, str, str | None]] = field(default_factory=list)
    reviews: list[ReviewSubmission] = field(default_factory=list)
    during_post: Callable[[], Awaitable[None]] | None = None

    async def upsert(self, target: CheckRunTarget, view: CheckRunView) -> None:
        self.check_runs.append((target.run_id, view.status, view.conclusion))

    async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview:
        self.reviews.append(submission)
        if self.during_post is not None:
            await self.during_post()
        return SubmittedReview(7001, tuple(9000 + n for n in range(len(submission.findings))))

    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> CiEligibility:
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
        github=GitHubAdapters(vcs=Vcs(), check_runs=github, reviews=github, eligibility=github),
        provider_factory=model.for_attempt,
        delays=SHORT_DELAYS,
    )
    run_q = await channels.consumer_queue(run_queue("fast"))
    publish_q = await channels.consumer_queue(PUBLISH_QUEUE)
    tasks = [
        asyncio.create_task(
            consume(run_q, partial(handle_run_delivery, handler=process.handle_run.execute))
        ),
        asyncio.create_task(
            consume(
                publish_q, partial(handle_publish_delivery, handler=process.publish_review.execute)
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
            await wait_for_state(factory, run_id, "succeeded")
            await wait_for_check_run(github, "completed")
            await asyncio.sleep(0.2)
        await engine.dispose()
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

    app.dependency_overrides[get_run_repository] = lambda: SqlAlchemyRunRepository(factory)
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

    async def scenario() -> None:
        first = asyncio.create_task(run_as_leader(engine, 42_034, 0.05, tick("first"), name="a"))
        await asyncio.sleep(0.3)
        second = asyncio.create_task(run_as_leader(engine, 42_034, 0.05, tick("second"), name="b"))
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
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    database_url = f"{env.database_url}?options=-csearch_path%3D{env.schema}"
    settings = WorkerSettings.from_environment(
        {"DATABASE_URL": database_url, "RABBITMQ_URL": env.rabbitmq_url}
    )

    async def scenario() -> bool:
        task = asyncio.create_task(run_worker(settings, leader_period=0.1))
        await asyncio.sleep(1.5)
        healthy = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return healthy

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(scenario()) is True
    assert "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set" in caplog.text
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
