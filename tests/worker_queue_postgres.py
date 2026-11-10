"""What the worker queue tests share: the topology reset, a migrated schema, the fakes (#34).

The ``env`` fixture reaches a test file through ``pytest_plugins``; the rest is imported.

Opt-in: set ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL``
(disposable RabbitMQ: the tests delete and redeclare the review topology).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.modules.code_index.infrastructure.amqp as index_amqp
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
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.prompt_builder import PullRequestMeta, ReviewContext
from app.modules.reviews.application.publish_run_review import ReviewSubmission, SubmittedReview
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_failures import RetryDelays, RunFailure
from app.modules.reviews.infrastructure.amqp import (
    DEAD_LETTER_EXCHANGE,
    DEAD_LETTER_QUEUE,
    ENGINES,
    EXCHANGE,
    PUBLISH_QUEUE,
    RETRY_EXCHANGE,
    RETRY_KEYS,
    AmqpChannels,
    retry_queue,
    run_queue,
)
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)
from app.worker import WorkerSettings
from tests.template_database import migrated_database

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
    try:
        with migrated_database(database_url, schema) as clone_url:
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
            engine = create_engine(clone_url, poolclass=NullPool)
            try:
                with engine.begin() as connection:
                    connection.execute(text(f'SET search_path TO "{schema}"'))
                    for statement, values in statements:
                        connection.execute(text(statement), values)
            finally:
                engine.dispose()
            yield Env(installation_id, clone_url, schema, rabbitmq_url, pr_id, rule_id, prompt_id)
    finally:
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


_APP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_APP_PEM = _APP_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()


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
