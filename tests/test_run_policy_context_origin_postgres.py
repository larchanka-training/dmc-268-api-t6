"""Policy/context provider reads stop at the next request after origin retirement."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import UUID

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import ConventionsRequest
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.handle_review_run import AttemptCheckpoint
from app.modules.reviews.application.prompt_builder import ReviewContext
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_context_stage import ReviewContextStage
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.run_trace import ContextTrace
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleUnitOfWork
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import GitHubAdapters, compose_worker_process, review_context_stage_factory
from tests.portal_postgres import HEAD, INSTALL_A, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_app_reinstall_postgres import FreshGitHub, apply
from tests.test_github_vcs_provider import _pull_payload
from tests.test_retired_installation_postgres import DiscardTokenCache
from tests.test_review_context import _GREET_PATCH, _MODELS, _SERVICE
from tests.test_run_origin_guard_postgres import prepare
from tests.test_run_publication_origin_postgres import retire
from tests.test_worker_queue_postgres import (
    SETTINGS,
    GitHub,
    Model,
    _no_run_source,
)

_BASE = "b" * 40
_REVIEW = "1" * 40
_RULES = "2" * 40
_AGENTS = "3" * 40
_APP = "4" * 40
_MODELS_SHA = "5" * 40


@pytest.fixture
def context_env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test context origin request boundaries")
    with portal_schema(url, None) as env:
        yield env


@pytest.mark.integration
@pytest.mark.parametrize(
    "boundary",
    [
        "policy_token",
        "policy_root",
        "policy_rules_blob",
        "pr_token",
        "comparison_token",
        "context_tree_token",
        "context_blob",
    ],
)
@pytest.mark.parametrize("origin", ["active", "deleted", "rebound"])
def test_composed_stage_stops_policy_context_requests_after_retirement(
    context_env: Env, boundary: str, origin: str
) -> None:
    async def exercise() -> None:
        engine = context_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []
        token_calls = 0
        stopped_at: int | None = None

        async def change(phase: str) -> None:
            nonlocal stopped_at
            if phase != boundary or origin == "active":
                return
            await retire(factory)
            if origin == "rebound":
                assert await apply(factory, FreshGitHub())
            stopped_at = len(calls)

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            path = request.url.path
            calls.append(request.method + " " + path)
            # Independent locks prove no provider transaction crosses the HTTP boundary.
            async with factory.begin() as session:
                for table, row_id in (
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                    ("runs", RUN_ATTEMPTED),
                ):
                    await session.execute(
                        text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": row_id},
                    )
            if path == "/app/installations/17/access_tokens":
                token_calls += 1
                await change(
                    {
                        1: "policy_token",
                        5: "pr_token",
                        6: "comparison_token",
                        7: "context_tree_token",
                    }.get(token_calls, "")
                )
                return httpx.Response(
                    201,
                    json={
                        "token": "token",
                        "expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            if "/git/trees/" in path:
                sha = path.rsplit("/", 1)[1]
                if sha == _BASE:
                    await change("policy_root")
                    tree = [
                        {"path": ".review", "mode": "040000", "type": "tree", "sha": _REVIEW},
                        {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": _AGENTS},
                    ]
                elif sha == _REVIEW:
                    tree = [{"path": "rules.md", "mode": "100644", "type": "blob", "sha": _RULES}]
                else:
                    assert sha == HEAD
                    tree = [
                        {"path": "pkg/service.py", "type": "blob", "sha": _APP},
                        {"path": "pkg/models.py", "type": "blob", "sha": _MODELS_SHA},
                    ]
                return httpx.Response(200, json={"tree": tree, "truncated": False})
            if "/git/blobs/" in path:
                sha = path.rsplit("/", 1)[1]
                await change({_RULES: "policy_rules_blob", _APP: "context_blob"}.get(sha, ""))
                raw = {
                    _RULES: b"rules",
                    _AGENTS: b"agents",
                    _APP: _SERVICE.encode(),
                    _MODELS_SHA: _MODELS.encode(),
                }[sha]
                return httpx.Response(
                    200,
                    json={
                        "sha": sha,
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode(),
                        "size": len(raw),
                    },
                )
            if path.endswith("/pulls/3"):
                payload = _pull_payload(changed_files=1, head_sha=HEAD)
                payload["number"] = 3
                base = payload["base"]
                assert isinstance(base, dict)
                base["sha"] = _BASE
                return httpx.Response(200, json=payload)
            if "/compare/" in path:
                return httpx.Response(200, json={"merge_base_commit": {"sha": "c" * 40}})
            raise AssertionError(f"unexpected request {request.url}")

        try:
            await prepare(factory, "active", "running")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("UPDATE runs SET base_sha=:base WHERE id=:id"),
                    {"base": _BASE, "id": RUN_ATTEMPTED},
                )
            runs = SqlAlchemyRunRepository(factory, allow_unscoped=True)
            run = await runs.get_run_vcs_input(RUN_ATTEMPTED)
            assert run is not None
            async with SqlAlchemyRunProcessingUnitOfWork(factory) as work:
                await StoreDiffSnapshot(work.repository).execute(
                    run_id=RUN_ATTEMPTED,
                    code_change_id=run.code_change_id,
                    head_sha=HEAD,
                    files=[
                        DiffSnapshot(
                            "pkg/service.py", _GREET_PATCH, _APP, "modified", None, 1, 0, 1
                        )
                    ],
                )
                await work.commit()
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                checkpoint = AttemptCheckpoint(
                    partial(SqlAlchemyRunLifecycleUnitOfWork, factory),
                    RUN_ATTEMPTED,
                    datetime.now(UTC) + timedelta(minutes=5),
                    lambda: datetime.now(UTC),
                )
                build_stage = review_context_stage_factory(
                    client=client,
                    tokens=tokens,
                    vcs=HttpGitHubVcsProvider(client=client, token_provider=tokens),
                    runs=runs,
                    session_factory=factory,
                    redis=None,
                )
                stage = build_stage(None, checkpoint)
                if origin == "active":
                    await stage.begin(RUN_ATTEMPTED)
                    assert await stage.run(RUN_ATTEMPTED) is not None
                else:
                    try:
                        with pytest.raises(RunCancelled, match="installation_unavailable"):
                            await stage.begin(RUN_ATTEMPTED)
                            await stage.run(RUN_ATTEMPTED)
                    finally:
                        assert stopped_at is not None
                        assert len(calls) == stopped_at
            assert not any("installations/29" in call for call in calls)
            if origin == "active":
                assert any(_MODELS_SHA in call for call in calls)
                assert len([c for c in calls if "/compare/" in c]) == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["active", "repository_unavailable", "deadline"])
def test_awaited_redis_tree_miss_rechecks_run_before_token_acquisition(
    context_env: Env, boundary: str
) -> None:
    from app.modules.reviews.application.run_failures import RunFailure
    from app.modules.reviews.infrastructure.github_revision_source import GitHubRevisionSource
    from app.modules.reviews.infrastructure.redis_context_cache import RedisTreeCache

    async def exercise() -> None:
        engine = context_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)
        calls: list[str] = []
        cache_reads: list[str] = []
        cache_writes: list[str] = []

        class RedisMiss:
            async def get(self, name: str) -> None:
                nonlocal now
                cache_reads.append(name)
                if boundary == "repository_unavailable":
                    async with factory.begin() as session:
                        await session.execute(
                            text(
                                "UPDATE repositories SET installation_available=false WHERE id=:id"
                            ),
                            {"id": REPO_A},
                        )
                elif boundary == "deadline":
                    await asyncio.sleep(0)
                    now = deadline
                return None

            async def set(self, name: str, value: str, ex: int | None = None) -> object:
                cache_writes.append(name)
                return True

        def http(request: httpx.Request) -> httpx.Response:
            calls.append(request.method + " " + request.url.path)
            if request.url.path == "/app/installations/17/access_tokens":
                return httpx.Response(
                    201,
                    json={
                        "token": "token",
                        "expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            assert request.url.path == f"/repos/octo/a/git/trees/{HEAD}"
            return httpx.Response(200, json={"tree": [], "truncated": False})

        try:
            await prepare(factory, "active", "running")
            run = await SqlAlchemyRunRepository(factory, allow_unscoped=True).get_run_vcs_input(
                RUN_ATTEMPTED
            )
            assert run is not None
            checkpoint = AttemptCheckpoint(
                partial(SqlAlchemyRunLifecycleUnitOfWork, factory),
                RUN_ATTEMPTED,
                deadline,
                lambda: now,
            )
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                source = GitHubRevisionSource(
                    client=client,
                    token_provider=tokens,
                    vcs=HttpGitHubVcsProvider(client=client, token_provider=tokens),
                    locator=run.locator,
                    checkpoint=checkpoint,
                    tree_cache=RedisTreeCache(RedisMiss(), repository_id=REPO_A),
                )
                if boundary == "active":
                    assert (await source.tree(HEAD)).complete
                elif boundary == "repository_unavailable":
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await source.tree(HEAD)
                else:
                    with pytest.raises(RunFailure) as failure:
                        await source.tree(HEAD)
                    assert failure.value.error_code == "deadline_exceeded"
            assert cache_reads == [f"tree:{REPO_A}:{HEAD}"]
            if boundary == "active":
                assert calls == [
                    "POST /app/installations/17/access_tokens",
                    f"GET /repos/octo/a/git/trees/{HEAD}",
                ]
                assert cache_writes == cache_reads
            else:
                assert calls == []
                assert cache_writes == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("origin", "rag"),
    [("active", False), ("deleted", False), ("active", True)],
    ids=["active", "deleted", "active-rag"],
)
def test_worker_composition_passes_claimed_checkpoint_to_context_stage(
    context_env: Env, origin: str, rag: bool
) -> None:
    """The public worker stops the next policy request after a claimed origin retires.

    With RAG on, the factory's stage opens the index through its gate and records it.
    """

    async def exercise() -> None:
        engine = context_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        observer = create_engine(
            context_env.database_url,
            connect_args={"options": f"-csearch_path={context_env.schema}"},
        )
        calls: list[str] = []
        stage_claims: list[tuple[object, ...]] = []
        published: list[ReviewPublishPointer] = []

        class Queue:
            async def publish_confirmed(
                self,
                message: PendingRunMessage,
                *,
                kind: RunPublicationKind = RunPublicationKind.QUEUED,
            ) -> None:
                raise AssertionError("the claimed run is already queued")

            async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
                raise AssertionError("the composed attempt must not retry")

            async def publish_review(self, pointer: ReviewPublishPointer) -> None:
                published.append(pointer)

        class ReviewModel(Model):
            async def draft_conventions(
                self, *, request: ConventionsRequest
            ) -> Mapping[str, object]:
                return {
                    **await super().draft_conventions(request=request),
                    "files": [{"path": "pkg/service.py", "relevance": "Changed module."}],
                }

            async def draft_review(self, *, context: ReviewContext) -> Mapping[str, object]:
                self.review_calls += 1
                return {
                    "findings": [],
                    "summary": {
                        "problem": "No problems found.",
                        "done_well": "The change is small.",
                        "effort": "none",
                    },
                }

        async def http(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(request.method + " " + path)
            # Independent connections must acquire all three locks during every HTTP call.
            async with factory.begin() as session:
                for table, row_id in (
                    ("provider_installations", INSTALL_A),
                    ("repositories", REPO_A),
                    ("runs", RUN_ATTEMPTED),
                ):
                    await session.execute(
                        text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": row_id},
                    )
            if path == "/app/installations/17/access_tokens":
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if "/git/trees/" in path:
                sha = path.rsplit("/", 1)[1]
                if sha == _BASE:
                    if origin != "active":
                        await retire(factory)
                    tree = [
                        {"path": ".review", "mode": "040000", "type": "tree", "sha": _REVIEW},
                        {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": _AGENTS},
                    ]
                elif sha == _REVIEW:
                    tree = [{"path": "rules.md", "mode": "100644", "type": "blob", "sha": _RULES}]
                else:
                    assert sha == HEAD
                    tree = [
                        {"path": "pkg/service.py", "type": "blob", "sha": _APP},
                        {"path": "pkg/models.py", "type": "blob", "sha": _MODELS_SHA},
                    ]
                return httpx.Response(200, json={"tree": tree, "truncated": False})
            if "/git/blobs/" in path:
                sha = path.rsplit("/", 1)[1]
                raw = {
                    _RULES: b"rules",
                    _AGENTS: b"agents",
                    _APP: _SERVICE.encode(),
                    _MODELS_SHA: _MODELS.encode(),
                }[sha]
                return httpx.Response(
                    200,
                    json={
                        "sha": sha,
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode(),
                        "size": len(raw),
                    },
                )
            if path.endswith("/pulls/3"):
                payload = _pull_payload(changed_files=1, head_sha=HEAD)
                payload["number"] = 3
                base = payload["base"]
                assert isinstance(base, dict)
                base["sha"] = _BASE
                return httpx.Response(200, json=payload)
            if path.endswith("/pulls/3/files"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "filename": "pkg/service.py",
                            "status": "modified",
                            "sha": _APP,
                            "additions": 1,
                            "deletions": 0,
                            "changes": 1,
                            "patch": _GREET_PATCH,
                        }
                    ],
                )
            if "/compare/" in path:
                return httpx.Response(200, json={"merge_base_commit": {"sha": "c" * 40}})
            raise AssertionError(f"unexpected request {request.url}")

        try:
            await prepare(factory, "active", "queued")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO prompt_versions "
                        "(id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.conventions',1,'conventions',:checksum,true)"
                    ),
                    {"id": UUID(int=151), "checksum": "d" * 64},
                )
                if rag:
                    await session.execute(
                        text("UPDATE repositories SET rag_enabled=true WHERE id=:id"),
                        {"id": REPO_A},
                    )
            model = ReviewModel(factory)
            github = GitHub()
            runs = SqlAlchemyRunRepository(factory, allow_unscoped=True)
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                vcs = HttpGitHubVcsProvider(client=client, token_provider=tokens)
                build_stage = review_context_stage_factory(
                    client=client,
                    tokens=tokens,
                    vcs=vcs,
                    runs=runs,
                    session_factory=factory,
                    redis=None,
                )

                def stage(
                    trace: ContextTrace | None, checkpoint: RunProviderCheckpoint
                ) -> ReviewContextStage:
                    # Observe the public composition input after the real claim commits.
                    with observer.connect() as connection:
                        row = connection.execute(
                            text("SELECT state::text,worker_id,attempt FROM runs WHERE id=:id"),
                            {"id": RUN_ATTEMPTED},
                        ).one()
                        stage_claims.append(tuple(row))
                    return build_stage(trace, checkpoint)

                process = compose_worker_process(
                    settings=SETTINGS,
                    session_factory=factory,
                    queue=Queue(),
                    github=GitHubAdapters(
                        vcs=vcs,
                        check_runs=github,
                        reviews=github,
                        eligibility=github,
                        run_source=_no_run_source,
                        context_stage=stage,
                    ),
                    provider_factory=model.for_attempt,
                )
                assert await process.handle_run.execute(RUN_ATTEMPTED) == "ack"
            assert stage_claims == [("running", "worker-it", 2)]
            assert all(
                call == "POST /app/installations/17/access_tokens"
                for call in calls
                if "access_tokens" in call
            )
            if origin != "active":
                assert calls == [
                    "POST /app/installations/17/access_tokens",
                    f"GET /repos/octo/a/git/trees/{_BASE}",
                ], "context-stage checkpoint allowed a request after retirement"
                assert model.review_calls == 0
                assert published == []
            else:
                assert f"GET /repos/octo/a/git/trees/{_REVIEW}" in calls
                assert f"GET /repos/octo/a/git/blobs/{_MODELS_SHA}" in calls
                assert model.review_calls == 1
                assert [pointer.run_id for pointer in published] == [RUN_ATTEMPTED]
            async with factory() as session:
                terminal = (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
                if origin == "active":
                    assert tuple(terminal) == ("publishing", None)
                    context = await session.scalar(
                        text(
                            "SELECT response FROM run_actions "
                            "WHERE run_id=:id AND tool='context.build'"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                    assert context is not None
                    assert context["status"] == "review"
                    assert any(block["path"] == "pkg/models.py" for block in context["blocks"])
                    steps = [
                        tuple(row)
                        for row in await session.execute(
                            text(
                                "SELECT tool,request,response FROM run_actions WHERE run_id=:id "
                                "AND tool IN ('context.build','context.rag') ORDER BY index"
                            ),
                            {"id": RUN_ATTEMPTED},
                        )
                    ]
                    tools = [step[0] for step in steps]
                    # The stage's context.build opens the attempt's context steps and is its
                    # only one (#168 part 6); one gate step follows it.
                    assert tools[0] == "context.build"
                    assert tools.count("context.build") == 1
                    assert tools.count("context.rag") == (1 if rag else 0)
                    # No state row yet: the gate reads an empty index and the review goes on.
                    if rag:
                        rag_step = steps[tools.index("context.rag")]
                        assert rag_step[1:] == (
                            {
                                "index_status": "empty",
                                "indexed_commit_sha": None,
                                "default_head_sha": None,
                                "stale": False,
                            },
                            {"outcome": "not_ready", "reason": "empty"},
                        )
                else:
                    assert tuple(terminal) == ("cancelled", "installation_unavailable")
        finally:
            observer.dispose()
            await engine.dispose()

    asyncio.run(exercise())
