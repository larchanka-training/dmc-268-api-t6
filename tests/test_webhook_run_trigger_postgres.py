"""The production webhook composition creates Runs from deliveries (#52, T1 and T6).

Built from the real ``ReviewsApiResources`` with a fake GitHub transport, so the test
fails if the dispatcher is composed without ``run_trigger``. Opt-in with
``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
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
from app.bootstrap.reviews_api import (
    ReviewsApiResources,
    get_github_webhook_receipt_uow_factory,
)
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.determine_ci_eligibility import DetermineCiEligibility
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.github_ci import HttpGitHubCurrentHeadCiProvider
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork

HEAD = "e" * 40
NEW_HEAD = "d" * 40
NEXT_HEAD = "c" * 40
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
    """Current PR and CI of PR 7; ``ci`` maps a head to its check suites.

    ``pull_request_status`` is the status the current-PR lookup answers with."""

    head: str = HEAD
    ci: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    pull_request_status: int = 200
    state: str = "open"
    labeled: bool = True
    updated_at: str = "2026-10-05T10:00:00Z"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo/repo/pulls/7":
            if self.pull_request_status != 200:
                return httpx.Response(self.pull_request_status, json={"message": "Server Error"})
            current = _pull_request(self.head)
            current["state"] = self.state
            current["labels"] = [{"name": "ai-review"}] if self.labeled else []
            current["updated_at"] = self.updated_at
            return httpx.Response(200, json=current)
        for sha, suites in ((sha, self.ci.get(sha, [])) for sha in (HEAD, NEW_HEAD, NEXT_HEAD)):
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
    if action in {"labeled", "unlabeled"}:
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
    fail: bool = False

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        if self.fail:
            raise RuntimeError("broker confirm unavailable")
        self.messages.append((message, kind))


class Pipeline:
    def __init__(self, database: Database, github: FakeGitHub) -> None:
        self.database = database
        self.github = github
        self.publisher = Publisher()
        self.deliveries = 0
        self._compose()

    def _compose(self) -> None:
        self.engine = create_async_engine(
            self.database.url,
            connect_args={"options": f"-csearch_path={self.database.schema}"},
            poolclass=NullPool,
        )
        self.factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        self.client = httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(self.github)
        )
        self.resources = ReviewsApiResources(self.engine, self.factory)
        self.receiver = self.resources.github_delivery_receiver(
            client=self.client,
            token_provider=StaticGitHubInstallationAccessTokenProvider(TOKEN),
            bot_login="reviewer[bot]",
            run_publisher=self.publisher,
            app_id=APP_ID,
        )

    async def deliver(self, event: str, payload: dict[str, Any]) -> None:
        self.deliveries += 1
        delivery = VerifiedGitHubDelivery(f"delivery-{self.deliveries}", event, payload)
        await self.receiver.execute(delivery.to_receipt())
        await self.replay()

    async def deliver_http(
        self, event: str, payload: dict[str, Any], *, delivery_id: str | None = None
    ) -> None:
        self.deliveries += 1
        body = json.dumps(payload).encode()
        secret = "issue109-test-webhook-secret"
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_github_webhook_secret] = lambda: secret
        app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: (
            self.resources.github_webhook_receipts
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": event,
                        "X-GitHub-Delivery": delivery_id or f"delivery-{self.deliveries}",
                        "X-Hub-Signature-256": f"sha256={signature}",
                        "Content-Type": "application/json",
                    },
                )
                assert response.status_code == 202, response.text
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(overrides)
        await self.replay()

    async def replay(self) -> int:
        return await self.receiver.replay_pending()

    async def restart(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()
        # Retain only the external publisher recorder and delivery count, not worker state.
        self._compose()

    async def due_retries(self) -> None:
        await self.sql(
            "UPDATE webhook_events SET retry_after = now() - interval '1 second' "
            "WHERE projected_at IS NULL"
        )

    async def replay_publications(self) -> int:
        enqueuer = TryEnqueueWebhookRun(
            eligibility=DetermineCiEligibility(
                candidates=SqlAlchemyEligibilityCandidateStore(self.factory),
                ci=HttpGitHubCurrentHeadCiProvider(
                    client=self.client,
                    token_provider=StaticGitHubInstallationAccessTokenProvider(TOKEN),
                ),
                own_app_id=APP_ID,
            ),
            uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(self.factory),
            publisher=self.publisher,
        )
        return await enqueuer.replay_pending_publications()

    async def sql(self, statement: str, **values: Any) -> list[tuple[Any, ...]]:
        async with self.factory.begin() as session:
            result = await session.execute(text(statement), values)
            return [tuple(row) for row in result] if statement.startswith("SELECT") else []

    async def runs(self) -> list[tuple[Any, ...]]:
        return await self.sql(
            "SELECT head_sha, state, message_published_at IS NOT NULL FROM runs ORDER BY created_at"
        )


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("GitHub webhook delivery ")
    ]


def _run(database: Database, github: FakeGitHub, scenario: Any) -> Any:
    async def main() -> Any:
        pipeline = Pipeline(database, github)
        try:
            return await scenario(pipeline)
        finally:
            await pipeline.client.aclose()
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
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    suites = [_suite(HEAD, 8, "queued", None, 0)]

    async def scenario(pipeline: Pipeline) -> list[tuple[Any, ...]]:
        await pipeline.sql("UPDATE repositories SET wait_for_ci = 'always'")
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        return await pipeline.runs()

    with caplog.at_level(logging.INFO):
        runs = _run(database, FakeGitHub(ci={HEAD: suites}), scenario)

    assert runs == []
    # The gate used to count that suite as blocking CI (`ci_blocked`); without it there is no CI
    # evidence yet, so `always` waits (`waiting_for_ci`). No Run is created either way.
    [line] = _outcome_lines(caplog)
    assert re.fullmatch(
        r"GitHub webhook delivery delivery-1 event=pull_request status=projected_pr "
        rf"detail=action=labeled pr={_UUID} head=eeeeeee: ineligible \(waiting_for_ci: no CI yet\)",
        line,
    ), line


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

    lines = _outcome_lines(caplog)
    pr = rf"pr={_UUID} head=eeeeeee"
    expected = [
        r"GitHub webhook delivery delivery-1 event=pull_request "
        r"status=ignored_unknown_repository detail=action=labeled unknown_repository "
        r"retry_at=\d{4}-\d\d-\d\dT\S+",
        rf"GitHub webhook delivery delivery-2 event=pull_request status=projected_pr "
        rf"detail=action=labeled {pr}: ineligible \(ci_blocked: check suite app=7 in_progress\)",
        rf"GitHub webhook delivery delivery-3 event=pull_request status=projected_pr "
        rf"detail=action=labeled {pr}: unconfigured \(missing_rules\)",
        rf"GitHub webhook delivery delivery-4 event=pull_request status=projected_pr "
        rf"detail=action=labeled {pr}: enqueued run={_UUID}",
        rf"GitHub webhook delivery delivery-5 event=pull_request status=projected_pr "
        rf"detail=action=labeled {pr}: duplicate \(active_run\)",
    ]
    assert len(lines) == len(expected), lines
    for line, pattern in zip(lines, expected, strict=True):
        assert re.fullmatch(pattern, line), line
    # The App was really called with the token: it appears in no message or record field.
    for logged in (caplog.text, *(repr(vars(record)) for record in caplog.records)):
        assert TOKEN not in logged


async def _link_installation_18(pipeline: Pipeline, *, with_repository: bool) -> None:
    """A second linked installation of the workspace, optionally storing repository 101 too."""
    await pipeline.sql(
        "INSERT INTO provider_installations (id, workspace_id, provider, external_id, metadata) "
        "SELECT :id, workspace_id, 'github', 18, '{}'::jsonb FROM provider_installations",
        id=uuid4(),
    )
    if with_repository:
        await pipeline.sql(
            "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
            "default_branch, web_url) SELECT :id, id, 101, 'octo/repo', 'main', "
            "'https://github.test/octo/repo' FROM provider_installations WHERE external_id = 18",
            id=uuid4(),
        )


async def _first_deferral(pipeline: Pipeline, delivery: dict[str, Any]) -> list[tuple[Any, ...]]:
    await pipeline.deliver("pull_request", delivery)
    return [
        *await pipeline.sql(
            "SELECT projection_attempt_count, retry_after IS NOT NULL, projection_deferred_at, "
            "projection_failed_at, projected_at FROM webhook_events"
        ),
        *await pipeline.sql("SELECT count(*) FROM code_changes"),
        *await pipeline.sql("SELECT count(*) FROM runs"),
    ]


@pytest.mark.integration
def test_a_label_on_a_disabled_repository_is_deferred_with_its_own_reason(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(pipeline: Pipeline) -> list[tuple[Any, ...]]:
        await pipeline.sql("UPDATE repositories SET enabled = false")
        # The same GitHub repository stored under another installation does not hide that the
        # event's own installation stores it disabled.
        await _link_installation_18(pipeline, with_repository=True)
        return await _first_deferral(pipeline, _pr_delivery("labeled"))

    with caplog.at_level(logging.INFO):
        state = _run(database, FakeGitHub(ci={HEAD: _green(HEAD)}), scenario)

    # Deferred for a retry like an unknown repository (first of three attempts), nothing stored.
    assert state == [(1, True, None, None, None), (0,), (0,)]
    [line] = _outcome_lines(caplog)
    assert re.fullmatch(
        r"GitHub webhook delivery delivery-1 event=pull_request "
        r"status=ignored_unknown_repository detail=action=labeled disabled_repository "
        r"retry_at=\d{4}-\d\d-\d\dT\S+",
        line,
    ), line


@pytest.mark.integration
@pytest.mark.parametrize("linked", [True, False], ids=["linked", "unlinked"])
def test_a_label_from_an_installation_that_does_not_store_the_repository_names_the_other_one(
    database: Database, caplog: pytest.LogCaptureFixture, linked: bool
) -> None:
    # Repository 101 is stored only under installation 17; the event comes from installation 18.
    # A label event is not checked against the linked installations, so 18 may be unlinked too.
    delivery = _pr_delivery("labeled")
    delivery["installation"] = {"id": 18}

    async def scenario(pipeline: Pipeline) -> list[tuple[Any, ...]]:
        if linked:
            await _link_installation_18(pipeline, with_repository=False)
        return await _first_deferral(pipeline, delivery)

    with caplog.at_level(logging.INFO):
        state = _run(database, FakeGitHub(ci={HEAD: _green(HEAD)}), scenario)

    assert state == [(1, True, None, None, None), (0,), (0,)]
    [line] = _outcome_lines(caplog)
    assert re.fullmatch(
        r"GitHub webhook delivery delivery-1 event=pull_request "
        r"status=ignored_unknown_repository detail=action=labeled other_installation_repository "
        r"retry_at=\d{4}-\d\d-\d\dT\S+",
        line,
    ), line


@pytest.mark.integration
def test_labeled_delivery_whose_github_call_fails_logs_its_action_and_category(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    github = FakeGitHub(ci={HEAD: _green(HEAD)}, pull_request_status=500)

    async def scenario(pipeline: Pipeline) -> list[tuple[Any, ...]]:
        await pipeline.deliver("pull_request", _pr_delivery("labeled"))
        return await pipeline.runs()

    with caplog.at_level(logging.INFO):
        runs = _run(database, github, scenario)

    assert runs == []
    failures = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and " failed stage=" in record.getMessage()
    ]
    assert failures == [
        "GitHub webhook delivery delivery-1 event=pull_request action=labeled "
        "failed stage=dispatch category=github_request error=HTTPStatusError"
    ]
    assert _outcome_lines(caplog) == []
    # The sweep logs the traceback after the line; neither record carries the token.
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "GitHub webhook projection failed for delivery delivery-1"
    ]
    for logged in (caplog.text, *(repr(vars(record)) for record in caplog.records)):
        assert TOKEN not in logged


@pytest.mark.integration
@pytest.mark.parametrize("terminal_before_ci", [False, True], ids=["race", "terminal-control"])
def test_successful_new_head_ci_survives_superseded_run_cancellation_without_another_event(
    database: Database, caplog: pytest.LogCaptureFixture, terminal_before_ci: bool
) -> None:
    github = FakeGitHub(ci={HEAD: _green(HEAD)})

    async def scenario(pipeline: Pipeline) -> None:
        await pipeline.deliver_http("pull_request", _pr_delivery("labeled"))
        await pipeline.sql("UPDATE runs SET state = 'running', attempt = 1")
        github.head = NEW_HEAD
        await pipeline.deliver_http("pull_request", _pr_delivery("synchronize", NEW_HEAD))
        assert await pipeline.sql("SELECT head_sha, state, cancel_requested FROM runs") == [
            ("e" * 40, "running", True)
        ]

        async def finalize_old_run() -> None:
            # Simulate worker finalization only; cancellation is requested by real projection.
            await pipeline.sql(
                "UPDATE runs SET state = 'cancelled', error_code = 'superseded', "
                "finished_at = now() WHERE head_sha = :head",
                head=HEAD,
            )

        if terminal_before_ci:
            await finalize_old_run()
        github.ci[NEW_HEAD] = _green(NEW_HEAD)
        await pipeline.deliver_http("check_suite", _check_suite(NEW_HEAD))
        assert await pipeline.sql("SELECT ci_status FROM code_changes") == [
            ({"event": "check_suite"},)
        ]
        assert await pipeline.sql("SELECT count(*) FROM webhook_events") == [(3,)]
        if not terminal_before_ci:
            assert await pipeline.runs() == [("e" * 40, "running", True)]
            await finalize_old_run()
        # Make a persisted retry due without a wall-clock wait or another delivery.
        await pipeline.sql("UPDATE webhook_events SET retry_after = now() - interval '1 second'")
        await pipeline.replay()
        assert pipeline.deliveries == 3
        assert await pipeline.sql("SELECT count(*) FROM webhook_events") == [(3,)]
        assert await pipeline.runs() == [
            ("e" * 40, "cancelled", True),
            ("d" * 40, "queued", True),
        ]
        assert [
            (message.head_sha, kind)
            for message, kind in pipeline.publisher.messages
            if kind == RunPublicationKind.QUEUED
        ] == [("e" * 40, RunPublicationKind.QUEUED), ("d" * 40, RunPublicationKind.QUEUED)]

    with caplog.at_level(logging.INFO):
        _run(database, github, scenario)
    if terminal_before_ci:
        assert any(
            "event=check_suite status=processed_ci" in line and ": enqueued run=" in line
            for line in _outcome_lines(caplog)
        )


async def _wait_for_new_head(pipeline: Pipeline) -> None:
    await pipeline.deliver_http("pull_request", _pr_delivery("labeled"))
    await pipeline.sql("UPDATE runs SET state = 'running', attempt = 1")
    pipeline.github.head = NEW_HEAD
    await pipeline.deliver_http("pull_request", _pr_delivery("synchronize", NEW_HEAD))
    pipeline.github.ci[NEW_HEAD] = _green(NEW_HEAD)
    await pipeline.deliver_http("check_suite", _check_suite(NEW_HEAD))
    assert await pipeline.sql("SELECT head_sha, state, cancel_requested FROM runs") == [
        ("e" * 40, "running", True)
    ]
    assert await pipeline.sql(
        "SELECT projected_at, projection_failed_at, projection_deferred_at, "
        "projection_attempt_count, retry_after IS NOT NULL, "
        "projection_claim_token, projection_lease_until FROM webhook_events "
        "WHERE delivery_id = 'delivery-3'"
    ) == [(None, None, None, 0, True, None, None)]


async def _finish_old_head(pipeline: Pipeline) -> None:
    # Worker terminal transition after successful B CI was durably processed while A ran.
    await pipeline.sql(
        "UPDATE runs SET state = 'cancelled', error_code = 'superseded', finished_at = now() "
        "WHERE head_sha = :head",
        head=HEAD,
    )


@pytest.mark.integration
@pytest.mark.parametrize("publication_fails", [False, True], ids=["confirmed", "outbox-recovery"])
def test_wait_survives_restart_redelivery_and_concurrent_replay_to_one_run(
    database: Database, caplog: pytest.LogCaptureFixture, publication_fails: bool
) -> None:
    async def scenario(pipeline: Pipeline) -> None:
        await _wait_for_new_head(pipeline)
        await pipeline.deliver_http("check_suite", _check_suite(NEW_HEAD), delivery_id="delivery-3")
        assert await pipeline.sql("SELECT count(*) FROM webhook_events") == [(3,)]
        # A distinct equivalent receipt also waits; neither may create another B.
        await pipeline.deliver_http("check_suite", _check_suite(NEW_HEAD))
        assert await pipeline.sql("SELECT count(*) FROM webhook_events") == [(4,)]
        for poll in range(4):
            if poll == 2:
                await pipeline.restart()
                assert await pipeline.replay() == 0  # retry_after survived the restart
            await pipeline.due_retries()
            assert await pipeline.replay() == 2
            assert await pipeline.sql(
                "SELECT count(*) FROM webhook_events WHERE projected_at IS NULL "
                "AND projection_failed_at IS NULL AND projection_deferred_at IS NULL "
                "AND projection_attempt_count = 0 AND retry_after IS NOT NULL"
            ) == [(2,)]
            assert await pipeline.runs() == [("e" * 40, "running", True)]
        deliveries = pipeline.deliveries
        await _finish_old_head(pipeline)
        pipeline.publisher.fail = publication_fails
        await pipeline.due_retries()
        await asyncio.gather(pipeline.replay(), pipeline.replay())
        assert pipeline.deliveries == deliveries == 5
        assert await pipeline.sql("SELECT count(*) FROM webhook_events") == [(4,)]
        assert await pipeline.runs() == [
            ("e" * 40, "cancelled", True),
            ("d" * 40, "queued", not publication_fails),
        ]
        assert await pipeline.sql(
            "SELECT count(*) FROM runs WHERE state IN ('queued', 'running', 'publishing')"
        ) == [(1,)]
        assert await pipeline.sql(
            "SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL"
        ) == [(4,)]
        if publication_fails:
            await pipeline.restart()
            pipeline.publisher.fail = False
            assert await pipeline.replay_publications() == 1
            assert await pipeline.replay_publications() == 0
        queued = [
            message
            for message, kind in pipeline.publisher.messages
            if kind == RunPublicationKind.QUEUED
        ]
        assert [message.head_sha for message in queued] == ["e" * 40, "d" * 40]
        assert len({message.run_id for message in queued}) == 2
        assert await pipeline.runs() == [("e" * 40, "cancelled", True), ("d" * 40, "queued", True)]
        await pipeline.sql(
            "UPDATE runs SET state = 'succeeded', finished_at = now() WHERE head_sha = :head",
            head=NEW_HEAD,
        )
        await pipeline.deliver_http("check_suite", _check_suite(NEW_HEAD))
        assert await pipeline.sql("SELECT count(*) FROM runs") == [(2,)]
        assert len(pipeline.publisher.messages) == 2

    with caplog.at_level(logging.INFO):
        _run(database, FakeGitHub(ci={HEAD: _green(HEAD)}), scenario)
    assert any("duplicate (head_already_reviewed)" in line for line in _outcome_lines(caplog))


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["always", "auto", "never"])
@pytest.mark.parametrize("invalidation", ["unlabeled", "closed", "disabled"])
def test_waiting_ci_receipt_rechecks_current_pr_label_and_repository(
    database: Database, mode: str, invalidation: str
) -> None:
    async def scenario(pipeline: Pipeline) -> None:
        await pipeline.sql("UPDATE repositories SET wait_for_ci = :mode", mode=mode)
        pipeline.github.ci[NEW_HEAD] = _green(NEW_HEAD)
        await _wait_for_new_head(pipeline)
        # An old labeled payload must use authoritative current PR/label state on retry too.
        await pipeline.deliver_http("pull_request", _pr_delivery("labeled", NEW_HEAD))
        assert await pipeline.sql(
            "SELECT projected_at FROM webhook_events WHERE delivery_id IN "
            "('delivery-2', 'delivery-4') ORDER BY delivery_id"
        ) == [(None,), (None,)]
        pipeline.github.updated_at = "2026-10-06T10:00:00Z"
        if invalidation == "unlabeled":
            pipeline.github.labeled = False
            await pipeline.deliver_http("pull_request", _pr_delivery("unlabeled", NEW_HEAD))
            assert await pipeline.sql("SELECT state, ai_review_labeled FROM code_changes") == [
                ("open", False)
            ]
        elif invalidation == "closed":
            pipeline.github.state = "closed"
            await pipeline.deliver_http("pull_request", _pr_delivery("closed", NEW_HEAD))
            assert await pipeline.sql("SELECT state FROM code_changes") == [("closed",)]
        else:
            await pipeline.sql("UPDATE repositories SET enabled = false")
        deliveries = pipeline.deliveries
        await _finish_old_head(pipeline)
        await pipeline.restart()
        await pipeline.due_retries()
        await pipeline.replay()
        assert pipeline.deliveries == deliveries
        assert await pipeline.runs() == [("e" * 40, "cancelled", True)]
        assert await pipeline.sql(
            "SELECT projected_at IS NOT NULL, retry_after FROM webhook_events "
            "WHERE delivery_id = 'delivery-3'"
        ) == [(True, None)]
        if invalidation != "disabled":
            assert await pipeline.sql(
                "SELECT projected_at IS NOT NULL FROM webhook_events "
                "WHERE delivery_id IN ('delivery-2', 'delivery-4') ORDER BY delivery_id"
            ) == [(True,), (True,)]
            expected_state = "closed" if invalidation == "closed" else "open"
            expected_label = invalidation == "closed"
            assert await pipeline.sql(
                "SELECT head_sha, state, ai_review_labeled FROM code_changes"
            ) == [("d" * 40, expected_state, expected_label)]
        assert [
            message.head_sha
            for message, kind in pipeline.publisher.messages
            if kind == RunPublicationKind.QUEUED
        ] == ["e" * 40]

    _run(database, FakeGitHub(ci={HEAD: _green(HEAD)}), scenario)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["always", "auto", "never"])
@pytest.mark.parametrize("ci_now", ["red", "pending"])
def test_waiting_ci_receipt_reads_live_ci_again_before_launch(
    database: Database, mode: str, ci_now: str
) -> None:
    async def scenario(pipeline: Pipeline) -> None:
        await pipeline.sql("UPDATE repositories SET wait_for_ci = :mode", mode=mode)
        await _wait_for_new_head(pipeline)
        pipeline.github.ci[NEW_HEAD] = [
            _suite(NEW_HEAD, 7, "completed", "failure", 1)
            if ci_now == "red"
            else _suite(NEW_HEAD, 7, "in_progress", None, 1)
        ]
        deliveries = pipeline.deliveries
        await _finish_old_head(pipeline)
        await pipeline.due_retries()
        await pipeline.replay()
        assert pipeline.deliveries == deliveries == 3
        expected = [("e" * 40, "cancelled", True)]
        if mode == "never":  # Existing contract deliberately ignores CI.
            expected.append(("d" * 40, "queued", True))
        assert await pipeline.runs() == expected
        assert await pipeline.sql(
            "SELECT projected_at IS NOT NULL, retry_after FROM webhook_events "
            "WHERE delivery_id = 'delivery-3'"
        ) == [(True, None)]

    _run(database, FakeGitHub(ci={HEAD: _green(HEAD)}), scenario)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["always", "auto", "never"])
def test_newer_head_during_wait_never_uses_obsolete_heads_successful_ci(
    database: Database, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(pipeline: Pipeline) -> None:
        await pipeline.sql("UPDATE repositories SET wait_for_ci = :mode", mode=mode)
        # Green B at synchronize time also leaves a PR receipt waiting in every mode.
        pipeline.github.ci[NEW_HEAD] = _green(NEW_HEAD)
        await _wait_for_new_head(pipeline)
        assert await pipeline.sql(
            "SELECT projected_at FROM webhook_events WHERE delivery_id = 'delivery-2'"
        ) == [(None,)]
        await pipeline.deliver_http("pull_request", _pr_delivery("labeled", NEW_HEAD))
        pipeline.github.head = NEXT_HEAD
        pipeline.github.updated_at = "2026-10-06T10:00:00Z"
        pipeline.github.ci[NEXT_HEAD] = [_suite(NEXT_HEAD, 7, "in_progress", None, 1)]
        await pipeline.deliver_http("pull_request", _pr_delivery("synchronize", NEXT_HEAD))
        deliveries = pipeline.deliveries
        await _finish_old_head(pipeline)
        await pipeline.restart()
        await pipeline.due_retries()
        await pipeline.replay()
        assert pipeline.deliveries == deliveries == 5
        assert await pipeline.sql(
            "SELECT head_sha, state, ai_review_labeled FROM code_changes"
        ) == [("c" * 40, "open", True)]
        assert await pipeline.sql(
            "SELECT count(*) FROM runs WHERE head_sha = :head", head=NEW_HEAD
        ) == [(0,)]
        expected = [("e" * 40, "cancelled", True)]
        if mode == "never":
            expected.append(("c" * 40, "queued", True))
        assert await pipeline.runs() == expected
        assert await pipeline.sql(
            "SELECT projected_at IS NOT NULL, retry_after FROM webhook_events "
            "WHERE delivery_id = 'delivery-3'"
        ) == [(True, None)]
        # Only C's own success can enable C in modes that consult CI.
        pipeline.github.ci[NEXT_HEAD] = _green(NEXT_HEAD)
        await pipeline.deliver_http("check_suite", _check_suite(NEXT_HEAD))
        assert await pipeline.runs() == [("e" * 40, "cancelled", True), ("c" * 40, "queued", True)]
        assert [
            message.head_sha
            for message, kind in pipeline.publisher.messages
            if kind == RunPublicationKind.QUEUED
        ] == ["e" * 40, "c" * 40]

    with caplog.at_level(logging.INFO):
        _run(database, FakeGitHub(ci={HEAD: _green(HEAD)}), scenario)
    assert any("no open pull request at this head" in line for line in _outcome_lines(caplog))
