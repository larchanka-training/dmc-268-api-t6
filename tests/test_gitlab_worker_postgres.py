"""Public installed worker on disposable PostgreSQL/RabbitMQ and synthetic HTTP."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app import worker as worker_module
from app.common.infrastructure.db.leader import run_as_leader
from app.modules.reviews.infrastructure.amqp import amqp_channels
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import WorkerSettings, run_worker
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import rig
from tests.test_gitlab_repository_sources import SourceRouter
from tests.test_gitlab_worker_outbox_postgres import pending_run
from tests.test_worker_queue_postgres import (
    LLM_ENV,
    SHORT_DELAYS,
    FakeLlm,
    _reset_topology,
    publish_run,
    wait_for_state,
)

worker_database = projection.authority_database
pytestmark = pytest.mark.integration


@pytest.fixture
def worker_stack(
    worker_database: tuple[str, str, UUID],
) -> Iterator[tuple[tuple[str, str, UUID], str]]:
    rabbit = os.environ.get("TEST_RABBITMQ_URL")
    if rabbit is None:
        pytest.skip("set TEST_RABBITMQ_URL for installed worker tests")
    # Only the explicitly reserved disposable broker is permitted.
    assert "127.0.0.1:58447/dmc169-test" in rabbit or os.environ.get("GITHUB_ACTIONS") == "true"
    asyncio.run(_reset_topology(rabbit))
    try:
        yield worker_database, rabbit
    finally:
        asyncio.run(_reset_topology(rabbit))


def runtime_env(url: str, rabbit: str) -> dict[str, str]:
    return {
        "DATABASE_URL": url,
        "RABBITMQ_URL": rabbit,
        "WORKER_ID": "installed-gitlab",
        "GITLAB_ACTIVE_KEY_ID": "test-key",
        "GITLAB_CREDENTIAL_KEYS": json.dumps({"test-key": base64.b64encode(b"k" * 32).decode()}),
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com"]',
        **LLM_ENV,
    }


@pytest.fixture
def worker_data_engines(monkeypatch: pytest.MonkeyPatch) -> list[AsyncEngine]:
    engines: list[AsyncEngine] = []

    def create_worker_engine(url: str, **kwargs: Any) -> AsyncEngine:
        engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=1, **kwargs)
        engines.append(engine)
        return engine

    async def dedicated_leader(
        engine: AsyncEngine,
        key: int,
        period: float,
        tick: Callable[[], Awaitable[object]],
        *,
        name: str,
    ) -> None:
        # The real leader keeps its AUTOCOMMIT lease. Give it a dedicated pool so
        # all actual worker data sessions compete for exactly one different lease.
        leader_engine = create_async_engine(engine.url, poolclass=NullPool)
        try:
            await run_as_leader(leader_engine, key, period, tick, name=name)
        finally:
            await leader_engine.dispose()

    monkeypatch.setattr(worker_module, "create_async_engine", create_worker_engine)
    monkeypatch.setattr(worker_module, "run_as_leader", dedicated_leader)
    return engines


async def assert_worker_data_lease_free(engine: AsyncEngine) -> None:
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    except SqlAlchemyTimeoutError as exc:
        raise AssertionError("worker data lease spans the HTTP boundary") from exc


async def check_transaction_oracle(engine: AsyncEngine, url: str) -> None:
    # Positive control: a proven open transaction in another task/PID cannot make
    # a request fail. This reproduces the global pg_stat_activity race from CI.
    unrelated = create_async_engine(url, poolclass=NullPool)
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def hold_unrelated_transaction() -> None:
        async with unrelated.begin() as connection:
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(pid, int)
            ready.set_result(pid)
            await release.wait()

    task = asyncio.create_task(hold_unrelated_transaction())
    try:
        pid = await asyncio.wait_for(ready, 5)
        async with unrelated.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT xact_start IS NOT NULL FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid},
                )
                is True
            )
        await assert_worker_data_lease_free(engine)
    finally:
        release.set()
        try:
            await task
        finally:
            await unrelated.dispose()

    # Negative control: even when the HTTP callback runs in a child task, a lease
    # still held by its caller must be detected; task-ID filtering would miss it.
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1
        with pytest.raises(AssertionError, match="worker data lease spans the HTTP boundary"):
            await asyncio.create_task(assert_worker_data_lease_free(engine))
    await assert_worker_data_lease_free(engine)


@pytest.mark.parametrize(
    "github_configured,model_configured", [(False, True), (True, True), (False, False)]
)
def test_public_worker_installed_gitlab_run_uses_shared_models_and_publisher(
    worker_stack: tuple[tuple[str, str, UUID], str],
    github_configured: bool,
    model_configured: bool,
    worker_data_engines: list[AsyncEngine],
) -> None:
    database, rabbit = worker_stack
    url, schema, _ = database

    async def scenario() -> None:
        async with asyncio.timeout(40), rig(database) as fixture:
            seeded = await pending_run(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET available_at=now(),created_at=now(),base_ref='main' "
                        "WHERE id=:id"
                    ),
                    {"id": seeded.run_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.conventions',1,'conventions',:checksum,true)"
                    ),
                    {"id": uuid4(), "checksum": "c" * 64},
                )
            source = SourceRouter()
            source.vcs.canonical["source_project_id"] = 17
            source.vcs.canonical["changes_count"] = "1"
            source.vcs.files = [
                {
                    "old_path": "src/app.py",
                    "new_path": "src/app.py",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                    "diff": "@@ -1 +1 @@\n-old\n+new\n",
                }
            ]
            published: list[dict[str, Any]] = []
            gh_calls: list[str] = []
            llm = FakeLlm()
            observed_boundary = False

            def model(request: httpx.Request) -> httpx.Response:
                response = llm(request)
                data = response.json()
                answer = json.loads(data["choices"][0]["message"]["content"])
                if "files" in answer:
                    answer["files"] = [{"path": "src/app.py", "relevance": "Changed module."}]
                else:
                    answer["findings"] = []
                data["choices"][0]["message"]["content"] = json.dumps(answer)
                return httpx.Response(200, json=data)

            async def external(request: httpx.Request) -> httpx.Response:
                nonlocal observed_boundary
                assert len(worker_data_engines) == 1
                engine = worker_data_engines[0]
                if not observed_boundary:
                    await check_transaction_oracle(engine, url)
                    observed_boundary = True
                # Borrow the actual worker pool, not just the fixture's separate pool.
                await assert_worker_data_lease_free(engine)
                assert request.url.host == "gitlab.com"
                assert request.headers["PRIVATE-TOKEN"] == "synthetic-authority-PAT-canary"
                # Public single-lease data pool remains available at a sampled HTTP boundary.
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": seeded.run_id},
                    )
                if request.url.path.endswith("/discussions"):
                    if request.method == "POST":
                        published.append(json.loads(request.content))
                        from tests.test_gitlab_vcs import reply

                        return reply({"notes": [{"id": 701}]}, status=201)
                    from tests.test_gitlab_vcs import reply

                    return reply([])
                return source.response(request)

            def reject_github(request: httpx.Request) -> httpx.Response:
                gh_calls.append(str(request.url))
                raise AssertionError("GitLab must never call GitHub")

            env = runtime_env(f"{url}?options=-csearch_path%3D{schema}", rabbit)
            if not model_configured:
                env = {key: value for key, value in env.items() if not key.startswith("LLM_")}
            if github_configured:
                from tests.test_worker_queue_postgres import _APP_PEM

                env.update({"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": _APP_PEM})
            worker = asyncio.create_task(
                run_worker(
                    WorkerSettings.from_environment(env),
                    delays=SHORT_DELAYS,
                    gitlab_transport_factory=lambda: httpx.MockTransport(external),
                    github_transport=httpx.MockTransport(reject_github),
                    llm_transport=httpx.MockTransport(model),
                )
            )
            try:
                async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                    await publish_run(channels, fixture.sessions, seeded.run_id)
                state = await wait_for_state(fixture.sessions, seeded.run_id, "succeeded", "failed")
                assert state == (
                    ("succeeded", 1, None) if model_configured else ("failed", 3, "llm_unavailable")
                ), [(r.method, str(r.url)) for r in source.requests]
                assert observed_boundary
                assert gh_calls == []
                assert llm.tasks == (
                    ["RepoConventionsDraft", "ReviewOutput"] if model_configured else []
                )
                assert len(published) == (1 if model_configured else 0)
                if model_configured:
                    assert published[0]["body"].endswith(
                        "<!-- ai-review findings_hash="
                        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 "
                        "summary -->"
                    )
                async with fixture.sessions() as session:
                    # The GitLab stage records the attempt's only context.build and writes
                    # its response as the run's context_payloads row (#168 part 6).
                    builds = await session.scalar(
                        text(
                            "SELECT count(*) FROM run_actions "
                            "WHERE run_id=:id AND tool='context.build'"
                        ),
                        {"id": seeded.run_id},
                    )
                    payloads = await session.scalar(
                        text("SELECT count(*) FROM context_payloads WHERE run_id=:id"),
                        {"id": seeded.run_id},
                    )
                # Without a model each of the three attempts builds before its conventions call.
                assert (builds, payloads) == ((1, 1) if model_configured else (3, 1))
                async with fixture.sessions() as session:
                    body = await session.scalar(
                        text("SELECT review_body FROM runs WHERE id=:id"), {"id": seeded.run_id}
                    )
                    if model_configured:
                        assert isinstance(body, str)
                        assert body == (
                            "## Review summary\n\n"
                            "**Verdict:** clean\n"
                            "**Risk summary:** critical: 0, high: 0, medium: 0, low: 0, info: 0\n"
                            "**Checked files:**\n- src/app.py\n\n"
                            "**Main issue:** Подтверждённых замечаний нет.\n\n"
                            "**Done well:** Проанализировано файлов с текстовыми изменениями: 1; "
                            "исключено файлов: 0.\n\n**Effort:** none"
                        )
                    else:
                        assert body is None
                    assert await session.scalar(
                        text("SELECT count(*) FROM usage_events WHERE run_id=:id"),
                        {"id": seeded.run_id},
                    ) == (2 if model_configured else 0)
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM run_policy_snapshots WHERE run_id=:id"),
                            {"id": seeded.run_id},
                        )
                        == 1
                    )
                assert (
                    await SqlAlchemyRunRepository(
                        fixture.sessions, allow_unscoped=True
                    ).get_run_vcs_input(seeded.run_id)
                    is not None
                )
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["unchanged", "route", "retired"])
def test_public_builder_refuses_changed_original_authority_with_pool_one(
    worker_database: tuple[str, str, UUID],
    mutation: str,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.bootstrap.gitlab_reviews import build_gitlab_review_worker_resources
    from app.modules.reviews.application.handle_review_run import ClaimedAttempt
    from app.modules.reviews.application.run_failures import RunCancelled

    async def scenario() -> None:
        async with asyncio.timeout(15), rig(worker_database) as fixture:
            seeded = await pending_run(fixture)
            from app.modules.reviews.infrastructure.run_lifecycle_store import (
                SqlAlchemyRunLifecycleUnitOfWork,
            )

            async with SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions) as uow:
                assert await uow.runs.lock_for_claim(seeded.run_id) is not None
                assert (
                    await uow.runs.claim(
                        seeded.run_id,
                        worker_id="test",
                        now=datetime.now(UTC),
                        lease_until=datetime.now(UTC) + timedelta(minutes=2),
                    )
                    == 1
                )
                await uow.commit()
            repository = SqlAlchemyRunRepository(fixture.sessions, allow_unscoped=True)
            expected = await repository.get_run_vcs_input(seeded.run_id)
            assert expected is not None
            settings = WorkerSettings.from_environment(
                runtime_env(worker_database[0], "amqp://unused")
            )
            assert settings.gitlab is not None
            calls: list[str] = []
            router = SourceRouter()

            async def external(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                # Same supplied data pool has one lease: every source read closes it.
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": seeded.run_id},
                    )
                return router.response(request)

            resources = build_gitlab_review_worker_resources(
                settings=settings.gitlab,
                session_factory=fixture.sessions,
                transport_factory=lambda: httpx.MockTransport(external),
            )
            claimed = ClaimedAttempt(
                seeded.run_id,
                fixture.workspace,
                1,
                "fast",
                datetime.now(UTC) + timedelta(minutes=1),
                seeded.prompt_id,
                seeded.rule_id,
                "test",
            )

            async def checkpoint() -> None:
                pass

            async with resources.attempt(
                claimed=claimed, expected_input=expected, checkpoint=checkpoint
            ) as bundle:
                if mutation == "route":
                    async with fixture.sessions.begin() as session:
                        await session.execute(
                            text(
                                "UPDATE repositories SET full_name='replacement/project' "
                                "WHERE id=:id"
                            ),
                            {"id": seeded.repository_id},
                        )
                elif mutation == "retired":
                    await fixture.lifecycle.revoke_installation(installation_id=seeded.origin_id)
                if mutation == "unchanged":
                    from app.modules.reviews.application.conventions import derive_languages

                    await bundle.context_stage.begin(seeded.run_id)
                    tree = await bundle.source.fetch_tree(seeded.repository_id)
                    assert tree.listing_complete and tree.sizes_complete
                    assert derive_languages(tree.files) == {"Python": 75, "TypeScript": 25}
                    tree_calls = [
                        r for r in router.requests if r.url.path.endswith("/repository/tree")
                    ]
                    assert tree_calls and all(r.url.params["ref"] == "b" * 40 for r in tree_calls)
                else:
                    with pytest.raises(RunCancelled):
                        await bundle.source.fetch_tree(seeded.repository_id)
            assert bool(calls) is (mutation == "unchanged")

    asyncio.run(scenario())


def test_public_worker_fails_a_gitlab_run_on_an_invalid_rules_md_without_llm(
    worker_stack: tuple[tuple[str, str, UUID], str],
) -> None:
    # RULES_FORMAT_SPEC §9 on a GitLab merge request: a review-ignore block without its
    # closing fence in the base `.review/rules.md` fails the run once in `policy.load`,
    # before the conventions model, and nothing is published.
    database, rabbit = worker_stack
    url, schema, _ = database

    async def scenario() -> None:
        async with asyncio.timeout(40), rig(database) as fixture:
            seeded = await pending_run(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET available_at=now(),created_at=now(),base_ref='main' "
                        "WHERE id=:id"
                    ),
                    {"id": seeded.run_id},
                )
                # A servable conventions prompt: without the policy error the model is called.
                await session.execute(
                    text(
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.conventions',1,'conventions',:checksum,true)"
                    ),
                    {"id": uuid4(), "checksum": "c" * 64},
                )
            source = SourceRouter()
            source.vcs.canonical["source_project_id"] = 17
            source.vcs.canonical["changes_count"] = "1"
            source.vcs.files = [
                {
                    "old_path": "src/app.py",
                    "new_path": "src/app.py",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                    "diff": "@@ -1 +1 @@\n-old\n+new\n",
                }
            ]
            # The router's default rules.md without its closing fence.
            source.files[".review/rules.md"] = ("2" * 40, b"```review-ignore\ngenerated/**\n")
            writes: list[str] = []
            gh_calls: list[str] = []
            llm = FakeLlm()

            async def external(request: httpx.Request) -> httpx.Response:
                assert request.url.host == "gitlab.com"
                if request.method != "GET":
                    writes.append(f"{request.method} {request.url.path}")
                return source.response(request)

            def reject_github(request: httpx.Request) -> httpx.Response:
                gh_calls.append(str(request.url))
                raise AssertionError("GitLab must never call GitHub")

            worker = asyncio.create_task(
                run_worker(
                    WorkerSettings.from_environment(
                        runtime_env(f"{url}?options=-csearch_path%3D{schema}", rabbit)
                    ),
                    delays=SHORT_DELAYS,
                    gitlab_transport_factory=lambda: httpx.MockTransport(external),
                    github_transport=httpx.MockTransport(reject_github),
                    llm_transport=httpx.MockTransport(llm),
                )
            )
            try:
                async with amqp_channels(rabbit, SHORT_DELAYS) as channels:
                    await publish_run(channels, fixture.sessions, seeded.run_id)
                state = await wait_for_state(fixture.sessions, seeded.run_id, "succeeded", "failed")
                # §9: failed at once, no retry.
                assert state == ("failed", 1, "policy_ignore_block_unclosed")
                assert llm.tasks == []
                assert writes == []
                assert gh_calls == []
                async with fixture.sessions() as session:
                    loads = (
                        await session.scalars(
                            text(
                                "SELECT response FROM run_actions "
                                "WHERE run_id=:id AND tool='policy.load'"
                            ),
                            {"id": seeded.run_id},
                        )
                    ).all()
                    snapshots = await session.scalar(
                        text("SELECT count(*) FROM run_policy_snapshots WHERE run_id=:id"),
                        {"id": seeded.run_id},
                    )
                # §9: the diagnostic is in the trace of `policy.load` and names the document
                # and the pinned base; a failed load stores no snapshot.
                [load] = loads
                diagnostic = load["error"]["diagnostic"]
                assert (
                    diagnostic["code"],
                    diagnostic["source_path"],
                    diagnostic["policy_base_sha"],
                ) == ("policy_ignore_block_unclosed", ".review/rules.md", "b" * 40)
                assert snapshots == 0
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    asyncio.run(scenario())
