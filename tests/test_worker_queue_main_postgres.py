"""The composition of the real worker main against real RabbitMQ and PostgreSQL.

``run_worker`` itself runs; GitHub and the model answer through ``httpx.MockTransport``.

Opt-in: set ``TEST_DATABASE_URL`` (disposable PostgreSQL) and ``TEST_RABBITMQ_URL``
(disposable RabbitMQ: the tests delete and redeclare the review topology).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

import aio_pika
import httpx
import pytest
from aiormq.exceptions import ChannelInvalidStateError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
)

import app.modules.code_index.infrastructure.amqp as index_amqp
import app.worker as worker_module
from app.common.application.leader_tasks import LeaderTasks
from app.modules.reviews.application.reconcile_runs import ReconcileRuns
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.infrastructure.amqp import (
    PUBLISH_QUEUE,
    AmqpChannels,
    amqp_channels,
    run_queue,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
)
from app.worker import WorkerSettings, run_worker
from tests.github_raw_blobs import blob_answer, git_blob_oid
from tests.worker_queue_postgres import (
    _APP_PEM,
    BASE,
    HEAD,
    LLM_ENV,
    PATCH,
    SHORT_DELAYS,
    Env,
    FakeLlm,
    insert_run,
    listen,
    publish_run,
    run_state,
    tools,
    wait_for_state,
)

pytest_plugins = ("tests.worker_queue_postgres",)

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
        if (
            self.agents_md is not None
            and path == f"{repo}/git/blobs/{git_blob_oid(self.agents_md)}"
        ):
            return blob_answer(request, self.agents_md)
        if path == f"{repo}/compare/{BASE}...{HEAD}":
            return httpx.Response(200, json={"merge_base_commit": {"sha": BASE}})
        if path == f"{repo}/git/trees/{BASE}":
            tree = [
                {"path": "README.md", "mode": "100644", "type": "blob", "sha": "c" * 40, "size": 3}
            ]
            if self.agents_md is not None:
                tree.append(
                    {
                        "path": "AGENTS.md",
                        "mode": "100644",
                        "type": "blob",
                        "sha": git_blob_oid(self.agents_md),
                        "size": 9,
                    }
                )
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
        # The leader's index tasks are not under test: converge would purge this index,
        # whose repository has RAG off (ADR §6.1), and fence out the active job.
        monkeypatch.setattr(worker_module, "index_leader_tasks", lambda *args: LeaderTasks([]))
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
