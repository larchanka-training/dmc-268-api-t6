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
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from aiormq.exceptions import ChannelPreconditionFailed
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.bootstrap.reviews_api import get_run_repository
from app.common.infrastructure.db.leader import run_as_leader
from app.common.infrastructure.heartbeat import is_fresh
from app.main import app
from app.modules.reviews.application.check_runs import (
    RunProviderCheckpoint,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.run_failures import RetryDelays
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
from tests.worker_queue_postgres import (
    BASE,
    HEAD,
    PATCH,
    SETTINGS,
    SHORT_DELAYS,
    Env,
    GitHub,
    Model,
    _no_run_source,
    insert_run,
    listen,
    publish_run,
    run_state,
    tools,
    wait_for_state,
)

pytest_plugins = ("tests.worker_queue_postgres",)


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


async def wait_for_check_run(github: GitHub, status: str, timeout: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not any(item[1] == status for item in github.check_runs):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no {status} check-run in {github.check_runs}")
        await asyncio.sleep(0.05)


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
            ).all()
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
    # A composition without the context stage records no context.build and no payload; the
    # per-provider rows are pinned with the stage (e2e and GitLab worker tests).
    assert payload == []
    assert run == (1, "worker-it", True)
    names = [action[0] for action in actions]
    assert "context.build" not in names
    for tool in ("vcs.fetch_diff", "review.finalize", "github.publish_review"):
        assert names.count(tool) == 1, names
        assert all(action[1] >= 0 for action in actions if action[0] == tool)
    assert names.count("llm.review_output") == 1
    assert names.index("review.finalize") < names.index("github.publish_review")
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
