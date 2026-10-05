"""The production webhook composition creates Runs from deliveries (#52, T1 and T6).

Built from the real ``ReviewsApiResources`` with a fake GitHub transport, so the test
fails if the dispatcher is composed without ``run_trigger``. Opt-in with
``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)

HEAD = "e" * 40
NEW_HEAD = "d" * 40
BASE = "b" * 40
APP_ID = 42
# A recognisable installation token: it must never show up in a log record.
TOKEN = "ghs_t72_sentinel"


@dataclass(frozen=True)
class Database:
    url: str
    schema: str


@pytest.fixture
def database() -> Iterator[Database]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_trigger_{uuid4().hex}"
    workspace, installation, repository = uuid4(), uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for statement, values in (
                (
                    "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'W', 0)",
                    {"id": workspace},
                ),
                (
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :ws, 'github', 17, '{}'::jsonb)",
                    {"id": installation, "ws": workspace},
                ),
                (
                    "INSERT INTO repositories (id, provider_installation_id, external_id, "
                    "full_name, default_branch, web_url) VALUES (:id, :installation, 101, "
                    "'octo/repo', 'main', 'https://github.test/octo/repo')",
                    {"id": repository, "installation": installation},
                ),
                (
                    "INSERT INTO rule_versions (id, repository_id, version, rules, checksum, "
                    "is_active) VALUES (:id, :repo, 1, '[]'::jsonb, :checksum, true)",
                    {"id": uuid4(), "repo": repository, "checksum": "a" * 64},
                ),
                (
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, "
                    "is_active) VALUES (:id, 'review.system', 1, 'system', :checksum, true)",
                    {"id": uuid4(), "checksum": "c" * 64},
                ),
            ):
                connection.execute(text(statement), values)
            connection.commit()
            yield Database(database_url, schema)
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@dataclass
class FakeGitHub:
    """Current PR and CI of PR 7; ``ci`` maps a head to its check suites."""

    head: str = HEAD
    ci: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo/repo/pulls/7":
            return httpx.Response(200, json=_pull_request(self.head))
        for sha, suites in ((sha, self.ci.get(sha, [])) for sha in (HEAD, NEW_HEAD)):
            if path == f"/repos/octo/repo/commits/{sha}/check-suites":
                return httpx.Response(
                    200, json={"total_count": len(suites), "check_suites": suites}
                )
            if path == f"/repos/octo/repo/commits/{sha}/status":
                return httpx.Response(200, json={"sha": sha, "state": "pending", "total_count": 0})
        raise AssertionError(f"unexpected GitHub call {request.method} {request.url}")


def _green(sha: str) -> list[dict[str, Any]]:
    return [
        {"id": 1, "head_sha": sha, "app": {"id": 7}, "status": "completed", "conclusion": "success"}
    ]


def _suite(sha: str, app_id: int, status: str, conclusion: str | None, runs: int) -> dict[str, Any]:
    return {
        "id": app_id,
        "head_sha": sha,
        "app": {"id": app_id},
        "status": status,
        "conclusion": conclusion,
        "latest_check_runs_count": runs,
    }


def _pull_request(head: str) -> dict[str, Any]:
    return {
        "id": 901,
        "number": 7,
        "title": "Add parser",
        "body": None,
        "html_url": "https://github.com/octo/repo/pull/7",
        "user": {"login": "alice"},
        "head": {"ref": "feature", "sha": head},
        "base": {"ref": "main", "sha": BASE},
        "state": "open",
        "merged": False,
        "updated_at": "2026-10-05T10:00:00Z",
        "labels": [{"name": "ai-review"}],
    }


def _pr_delivery(action: str, head: str = HEAD) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "number": 7,
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "sender": {"type": "User"},
        "pull_request": _pull_request(head),
    }
    if action == "labeled":
        payload["label"] = {"name": "ai-review"}
    return payload


def _check_suite(head: str) -> dict[str, Any]:
    return {
        "action": "completed",
        "installation": {"id": 17},
        "repository": {"id": 101},
        "check_suite": {"head_sha": head},
    }


@dataclass
class Publisher:
    messages: list[tuple[PendingRunMessage, RunPublicationKind]] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        self.messages.append((message, kind))


class Pipeline:
    def __init__(self, database: Database, github: FakeGitHub) -> None:
        self.engine = create_async_engine(
            database.url,
            connect_args={"options": f"-csearch_path={database.schema}"},
            poolclass=NullPool,
        )
        self.factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        self.github = github
        self.publisher = Publisher()
        self.deliveries = 0

    async def deliver(self, event: str, payload: dict[str, Any]) -> None:
        self.deliveries += 1
        client = httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(self.github)
        )
        receiver = ReviewsApiResources(self.engine, self.factory).github_delivery_receiver(
            client=client,
            token_provider=StaticGitHubInstallationAccessTokenProvider(TOKEN),
            bot_login="reviewer[bot]",
            run_publisher=self.publisher,
            app_id=APP_ID,
        )
        delivery = VerifiedGitHubDelivery(f"delivery-{self.deliveries}", event, payload)
        await receiver.execute(delivery.to_receipt())
        await receiver.replay_pending()
        await client.aclose()

    async def sql(self, statement: str, **values: Any) -> list[tuple[Any, ...]]:
        async with self.factory.begin() as session:
            result = await session.execute(text(statement), values)
            return [tuple(row) for row in result] if statement.startswith("SELECT") else []

    async def runs(self) -> list[tuple[Any, ...]]:
        return await self.sql(
            "SELECT head_sha, state, message_published_at IS NOT NULL FROM runs ORDER BY created_at"
        )


def _run(database: Database, github: FakeGitHub, scenario: Any) -> Any:
    async def main() -> Any:
        pipeline = Pipeline(database, github)
        try:
            return await scenario(pipeline)
        finally:
            await pipeline.engine.dispose()

    return asyncio.run(main())


@pytest.mark.integration
def test_labeled_pr_with_green_ci_creates_and_publishes_a_run(database: Database) -> None:
    async def scenario(pipeline: Pipeline) -> tuple[list[tuple[Any, ...]], Publisher]:
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        return await pipeline.runs(), pipeline.publisher

    runs, publisher = _run(database, FakeGitHub(ci={HEAD: _green(HEAD)}), scenario)

    assert runs == [(HEAD, "queued", True)]
    assert [(message.head_sha, kind) for message, kind in publisher.messages] == [
        (HEAD, RunPublicationKind.QUEUED)
    ]


@pytest.mark.integration
def test_queued_foreign_suites_without_runs_do_not_block_a_run_next_to_green_ci(
    database: Database,
) -> None:
    # Our own suite, a second reviewer App that never creates a check run (its suite stays
    # queued with no runs for good), and green Actions: the gate lets the review start (#72).
    suites = [
        _suite(HEAD, APP_ID, "queued", None, 0),
        _suite(HEAD, 8, "queued", None, 0),
        _suite(HEAD, 7, "completed", "success", 1),
    ]

    async def scenario(pipeline: Pipeline) -> tuple[list[tuple[Any, ...]], Publisher]:
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        return await pipeline.runs(), pipeline.publisher

    runs, publisher = _run(database, FakeGitHub(ci={HEAD: suites}), scenario)

    assert runs == [(HEAD, "queued", True)]
    assert [(message.head_sha, kind) for message, kind in publisher.messages] == [
        (HEAD, RunPublicationKind.QUEUED)
    ]


@pytest.mark.integration
def test_queued_foreign_suite_without_runs_alone_is_not_ci_evidence_for_wait_for_ci_always(
    database: Database,
) -> None:
    suites = [_suite(HEAD, 8, "queued", None, 0)]

    async def scenario(pipeline: Pipeline) -> list[tuple[Any, ...]]:
        await pipeline.sql("UPDATE repositories SET wait_for_ci = 'always'")
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        return await pipeline.runs()

    assert _run(database, FakeGitHub(ci={HEAD: suites}), scenario) == []


@pytest.mark.integration
def test_wait_for_ci_never_starts_without_ci(database: Database) -> None:
    async def scenario(pipeline: Pipeline) -> list[tuple[Any, ...]]:
        await pipeline.sql("UPDATE repositories SET wait_for_ci = 'never'")
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        return await pipeline.runs()

    assert _run(database, FakeGitHub(), scenario) == [(HEAD, "queued", True)]


@pytest.mark.integration
def test_wait_for_ci_always_waits_for_the_check_suite_event(database: Database) -> None:
    github = FakeGitHub()

    async def scenario(pipeline: Pipeline) -> tuple[list[tuple[Any, ...]], ...]:
        await pipeline.sql("UPDATE repositories SET wait_for_ci = 'always'")
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        before = await pipeline.runs()
        github.ci[HEAD] = _green(HEAD)
        await pipeline.deliver("check_suite", _check_suite(HEAD))
        ci_status = await pipeline.sql("SELECT ci_status FROM code_changes")
        return before, await pipeline.runs(), ci_status

    before, after, ci_status = _run(database, github, scenario)

    assert before == []
    assert after == [(HEAD, "queued", True)]
    assert ci_status == [({"event": "check_suite"},)]


@pytest.mark.integration
def test_check_suite_after_the_no_ci_sweep_excluded_the_head_creates_a_run(
    database: Database,
) -> None:
    github = FakeGitHub()

    async def scenario(pipeline: Pipeline) -> list[tuple[Any, ...]]:
        # A long CI: the label lands before any suite, then the sweep excluded the head.
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        await pipeline.sql('UPDATE code_changes SET ci_status = \'{"sweep": "excluded"}\'')
        github.ci[HEAD] = _green(HEAD)
        await pipeline.deliver("check_suite", _check_suite(HEAD))
        return await pipeline.runs()

    assert _run(database, github, scenario) == [(HEAD, "queued", True)]


@pytest.mark.integration
def test_synchronize_cancels_the_attempted_run_and_signals_the_worker_at_once(
    database: Database,
) -> None:
    github = FakeGitHub(ci={HEAD: _green(HEAD)})

    async def scenario(pipeline: Pipeline) -> tuple[list[tuple[Any, ...]], Publisher]:
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        # The worker claimed it once and put it back for a retry.
        await pipeline.sql("UPDATE runs SET attempt = 1")
        github.head = NEW_HEAD
        await pipeline.deliver("pull_request", _pr_delivery("synchronize", NEW_HEAD))
        return await pipeline.runs(), pipeline.publisher

    runs, publisher = _run(database, github, scenario)

    assert runs[0][:2] == (HEAD, "cancelled")
    kinds = [(message.head_sha, kind) for message, kind in publisher.messages]
    # The close signal goes out with the delivery, without the worker's replay.
    assert (HEAD, RunPublicationKind.CANCELLATION) in kinds
    assert kinds[0] == (HEAD, RunPublicationKind.QUEUED)


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@pytest.mark.integration
def test_every_label_outcome_is_logged_with_its_reason_and_without_the_token(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    github = FakeGitHub()
    unknown_repository = _pr_delivery("labeled")
    unknown_repository["repository"] = {"id": 999, "full_name": "octo/repo"}

    async def scenario(pipeline: Pipeline) -> None:
        await pipeline.deliver("pull_request", unknown_repository)
        github.ci[HEAD] = [_suite(HEAD, 7, "in_progress", None, 1)]
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        github.ci[HEAD] = _green(HEAD)
        await pipeline.sql("UPDATE rule_versions SET is_active = false")
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        await pipeline.sql("UPDATE rule_versions SET is_active = true")
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))

    with caplog.at_level(logging.INFO):
        _run(database, github, scenario)

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("GitHub webhook delivery ")
    ]
    pr = rf"pr={_UUID} head=eeeeeee"
    expected = [
        r"GitHub webhook delivery delivery-1 event=pull_request "
        r"status=ignored_unknown_repository detail=unknown_repository "
        r"retry_at=\d{4}-\d\d-\d\dT\S+",
        rf"GitHub webhook delivery delivery-2 event=pull_request status=projected_pr "
        rf"detail={pr}: ineligible \(ci_blocked\)",
        rf"GitHub webhook delivery delivery-3 event=pull_request status=projected_pr "
        rf"detail={pr}: unconfigured \(missing_rules\)",
        rf"GitHub webhook delivery delivery-4 event=pull_request status=projected_pr "
        rf"detail={pr}: enqueued run={_UUID}",
        rf"GitHub webhook delivery delivery-5 event=pull_request status=projected_pr "
        rf"detail={pr}: duplicate \(active_run\)",
    ]
    assert len(lines) == len(expected), lines
    for line, pattern in zip(lines, expected, strict=True):
        assert re.fullmatch(pattern, line), line
    # The App was really called with the token: it appears in no message or record field.
    for logged in (caplog.text, *(repr(vars(record)) for record in caplog.records)):
        assert TOKEN not in logged
