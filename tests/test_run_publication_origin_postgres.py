"""Retirement at publication/check-run HTTP and final PostgreSQL write boundaries."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application.check_runs import (
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.handle_review_run import update_check_run
from app.modules.reviews.application.publish_run_review import (
    PublishRunReview,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.github_review_publication import (
    GitHubCheckRunGateway,
    GitHubPullRequestReviewGateway,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import HEAD, INSTALL_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_app_reinstall_postgres import FreshGitHub, apply
from tests.test_publish_run_review import Trace
from tests.test_retired_installation_postgres import DiscardTokenCache
from tests.test_run_origin_guard_postgres import prepare


@pytest.fixture
def publication_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def retire(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE provider_installations SET deleted_at=clock_timestamp() WHERE id=:id"),
            {"id": INSTALL_A},
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "operation,stage",
    [
        ("check", "token"),
        ("check", "lookup"),
        ("check", "patch"),
        ("review", "token"),
        ("review", "lookup"),
        ("review", "head"),
        ("review", "post"),
        ("review", "last"),
        ("review", "marker"),
        ("review", "retry"),
        ("review", "coordinates"),
        ("review", "rebind"),
    ],
)
@pytest.mark.parametrize("retired", [False, True])
def test_retirement_during_token_stops_next_http_request_with_live_positive_control(
    publication_env: Env,
    operation: str,
    stage: str,
    retired: bool,
) -> None:
    async def exercise() -> None:
        engine = publication_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []

        posts = 0
        marker_hash = ""

        async def change(phase: str) -> None:
            if retired and stage == phase:
                await retire(factory)
                if phase == "rebind":
                    await apply(factory, FreshGitHub())

        async def sleep(seconds: float) -> None:
            await change("retry")

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal posts
            path = request.url.path
            calls.append(request.method + " " + path)
            # Every actual network entry sees a closed provider checkpoint UOW.
            async with factory.begin() as session:
                await session.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": RUN_ATTEMPTED},
                )
            if path == "/app/installations/17/access_tokens":
                await change("token")
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if path.endswith("/check-runs"):
                if request.method == "GET":
                    await change("patch" if stage == "patch" else "lookup")
                    return httpx.Response(
                        200,
                        json={
                            "check_runs": [{"id": 55, "external_id": str(RUN_ATTEMPTED)}]
                            if stage == "patch"
                            else []
                        },
                    )
                return httpx.Response(201, json={"id": 55})
            if path.endswith("/check-runs/55"):
                assert request.method == "PATCH"
                return httpx.Response(200, json={"id": 55})
            if path.endswith("/reviews"):
                if request.method == "GET":
                    await change("marker" if stage == "marker" else "lookup")
                    return httpx.Response(
                        200,
                        json=[{"id": 55, "body": f"<!-- ai-review findings_hash={marker_hash} -->"}]
                        if stage == "marker"
                        else [],
                    )
                posts += 1
                await change("post")
                await change("rebind")
                if posts == 1 and stage in ("retry", "coordinates"):
                    await change("coordinates")
                    return httpx.Response(
                        503 if stage == "retry" else 422, json={"message": "line invalid"}
                    )
                return httpx.Response(201, json={"id": 55})
            if path.endswith("/comments"):
                await change("last")
                return httpx.Response(200, json=[{"id": 900}])
            if path.endswith("/pulls/3"):
                await change("head")
                return httpx.Response(200, json={"head": {"sha": HEAD}})
            raise AssertionError(f"unexpected {request.method} {path}")

        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO findings "
                        "(id,run_id,file_path,line_start,severity,confidence,category,title,body,published,inline_comment)"
                        " VALUES "
                        "(:id,:run,'a.py',3,'medium',0.8,'correctness','T','B',false,true)"
                    ),
                    {"id": UUID(int=11399), "run": RUN_ATTEMPTED},
                )
                before_finding = await session.scalar(
                    text("SELECT to_jsonb(f) FROM findings f WHERE id=:id"), {"id": UUID(int=11399)}
                )
                before_terminal = (
                    (
                        await session.execute(
                            text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    )
                    .tuples()
                    .all()
                )
                await session.execute(
                    text(
                        "UPDATE runs SET "
                        "state=:state,origin_provider_installation_id=:origin "
                        "WHERE id=:id"
                    ),
                    {
                        "state": "publishing" if operation == "review" else "succeeded",
                        "origin": INSTALL_A,
                        "id": RUN_ATTEMPTED,
                    },
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
                    now=time.time,
                )

                def uow() -> SqlAlchemyRunLifecycleUnitOfWork:
                    return SqlAlchemyRunLifecycleUnitOfWork(factory)

                if operation == "check":
                    await update_check_run(
                        uow,
                        GitHubCheckRunGateway(client=client, token_provider=tokens),
                        RUN_ATTEMPTED,
                        lambda _: None,
                    )
                else:
                    async with factory() as session:
                        context = await SqlAlchemyRunLifecycleStore(session).publish_context(
                            RUN_ATTEMPTED
                        )
                        assert context is not None
                    marker_hash = context.findings_hash
                    publisher = PublishRunReview(
                        uow_factory=uow,
                        reviews=GitHubPullRequestReviewGateway(
                            client=client, token_provider=tokens
                        ),
                        check_runs=None,
                        trace=TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory)),
                        sleep=sleep,
                    )
                    pointer = ReviewPublishPointer(
                        RUN_ATTEMPTED, HEAD, context.findings_hash, "COMMENT"
                    )
                    await publisher.execute(pointer)
                    await publisher.execute(pointer)  # Terminal redelivery starts no new HTTP.
            token = "POST /app/installations/17/access_tokens"
            lookup = "GET /repos/octo/a/pulls/3/reviews"
            head = "GET /repos/octo/a/pulls/3"
            post = "POST /repos/octo/a/pulls/3/reviews"
            comments = "GET /repos/octo/a/pulls/3/reviews/55/comments"
            if operation == "check":
                expected = [
                    token,
                    "GET /repos/octo/a/commits/" + HEAD + "/check-runs",
                    "PATCH /repos/octo/a/check-runs/55"
                    if stage == "patch"
                    else "POST /repos/octo/a/check-runs",
                ]
            elif stage == "marker":
                expected = [token, lookup, comments]
            elif stage in ("retry", "coordinates"):
                expected = [token, lookup, head, post, token, lookup, head, post, comments]
            else:
                expected = [token, lookup, head, post, comments]
            stopped_after = {
                "token": 1,
                "lookup": 2,
                "patch": 2,
                "marker": 2,
                "head": 3,
                "post": 4,
                "retry": 4,
                "coordinates": 4,
                "rebind": 4,
                "last": 5,
            }
            assert calls == (expected[: stopped_after[stage]] if retired else expected)
            async with factory() as session:
                state = (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
                assert tuple(state) == (
                    (
                        "cancelled",
                        "installation_replaced"
                        if stage == "rebind"
                        else "installation_unavailable",
                    )
                    if retired and operation == "review"
                    else ("succeeded", None)
                )
                if retired and operation == "review" and stage in ("post", "rebind", "last"):
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM run_actions WHERE run_id=:id "
                                "AND tool='github.publish_review'"
                            ),
                            {"id": RUN_ATTEMPTED},
                        )
                        == 1
                    )
                    assert await session.scalar(
                        text(
                            "SELECT response FROM run_actions WHERE run_id=:id "
                            "AND tool='github.publish_review'"
                        ),
                        {"id": RUN_ATTEMPTED},
                    ) == {"github_review_id": 55}
                if retired:
                    assert (
                        await session.scalar(
                            text("SELECT to_jsonb(f) FROM findings f WHERE id=:id"),
                            {"id": UUID(int=11399)},
                        )
                        == before_finding
                    )
                    assert await session.scalar(text("SELECT count(*) FROM comments")) == 0
                elif operation == "review":
                    assert (
                        await session.scalar(
                            text("SELECT published FROM findings WHERE id=:id"),
                            {"id": UUID(int=11399)},
                        )
                        is True
                    )
                    assert await session.scalar(text("SELECT count(*) FROM comments")) == (
                        0 if stage == "coordinates" else 1
                    )
                assert (
                    await session.execute(
                        text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).tuples().all() == before_terminal
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    "origin,boundary",
    [
        (origin, boundary)
        for origin in ("retired", "rebound", "unknown")
        for boundary in ("consumer", "finalize", "stale", "unconfigured")
    ]
    + [("active", "stale"), ("active", "unconfigured")],
)
def test_inactive_publication_has_no_target_and_terminalizes_without_payload_writes(
    publication_env: Env,
    origin: str,
    boundary: str,
) -> None:
    async def exercise() -> None:
        engine = publication_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []

        async def http(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise AssertionError("inactive publication must not start provider HTTP")

        try:
            await prepare(factory, origin, "publishing")
            async with factory() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(RUN_ATTEMPTED)
                assert context is not None
                assert context.origin_active is (origin == "active")
                assert context.installation_id == (17 if origin == "active" else None)
                assert await SqlAlchemyRunLifecycleStore(session).provider_origin_active(
                    RUN_ATTEMPTED
                ) is (origin == "active")
                whole_before = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
                before = (
                    (
                        await session.execute(
                            text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    )
                    .tuples()
                    .all()
                )
                findings = (
                    (
                        await session.execute(
                            text("SELECT id,to_jsonb(f) FROM findings f ORDER BY id")
                        )
                    )
                    .tuples()
                    .all()
                )
            if boundary == "finalize":
                async with factory.begin() as session:
                    result = await SqlAlchemyRunLifecycleStore(session).complete_publication(
                        RUN_ATTEMPTED,
                        review_id=55,
                        comment_ids=(900,),
                        findings_hash=context.findings_hash,
                        moved_to_body=True,
                        now=datetime.now(UTC),
                    )
                    assert result == "cancelled"
            else:
                async with httpx.AsyncClient(
                    base_url="https://api.github.test", transport=httpx.MockTransport(http)
                ) as client:
                    tokens = GitHubAppInstallationAccessTokenProvider(
                        client=client,
                        app_id="42",
                        private_key="unused",
                        jwt_encoder=lambda claims, key: "jwt",
                        cache=DiscardTokenCache(),
                        now=time.time,
                    )
                    await PublishRunReview(
                        uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                        reviews=None
                        if boundary == "unconfigured"
                        else GitHubPullRequestReviewGateway(client=client, token_provider=tokens),
                        check_runs=None,
                        trace=Trace(),
                    ).execute(
                        ReviewPublishPointer(
                            RUN_ATTEMPTED,
                            HEAD,
                            "f" * 64 if boundary == "stale" else context.findings_hash,
                            "COMMENT",
                        )
                    )
            assert calls == []
            async with factory() as session:
                state = (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
                assert tuple(state) == (
                    ("publishing", None)
                    if origin == "active"
                    else ("cancelled", "installation_unavailable")
                )
                if origin == "active":
                    assert (
                        await session.scalar(
                            text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"),
                            {"id": RUN_ATTEMPTED},
                        )
                        == whole_before
                    )
                assert (
                    await session.execute(
                        text("SELECT id,to_jsonb(r) FROM runs r WHERE id<>:id ORDER BY id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).tuples().all() == before
                assert (
                    await session.execute(text("SELECT id,to_jsonb(f) FROM findings f ORDER BY id"))
                ).tuples().all() == findings
                assert await session.scalar(text("SELECT count(*) FROM comments")) == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


class GitLabReviews:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def submit_review(
        self, submission: ReviewSubmission, *, project_id: int
    ) -> SubmittedReview:
        self.calls.append(project_id)
        return SubmittedReview(880, ())


class ObservedCheckRuns:
    def __init__(self) -> None:
        self.calls: list[CheckRunTarget] = []

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        self.calls.append(target)
        raise AssertionError("GitLab publication must not write GitHub check-runs")


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["active", "unknown"])
def test_gitlab_run_publishes_only_with_an_active_origin(
    publication_env: Env,
    origin: str,
) -> None:
    async def exercise() -> None:
        engine = publication_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        gitlab = GitLabReviews()
        check_runs = ObservedCheckRuns()
        trace = Trace()
        try:
            await prepare(factory, origin, "publishing")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET provider='gitlab' WHERE id=:id"),
                    {"id": INSTALL_A},
                )
            async with factory() as session:
                store = SqlAlchemyRunLifecycleStore(session)
                context = await store.publish_context(RUN_ATTEMPTED)
                assert context is not None
                assert (context.provider, context.project_id) == ("gitlab", 101)
                assert context.origin_active is (origin == "active")
                guard = await store.lock_for_claim(RUN_ATTEMPTED)
                assert guard is not None
                assert guard.origin_active is (origin == "active")
            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                reviews=None,
                check_runs=check_runs,
                trace=trace,
                gitlab_reviews=gitlab,
            ).execute(ReviewPublishPointer(RUN_ATTEMPTED, HEAD, context.findings_hash, "COMMENT"))
            async with factory() as session:
                row = (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": RUN_ATTEMPTED},
                    )
                ).one()
            if origin == "active":
                assert tuple(row) == ("succeeded", None)
                assert gitlab.calls == [101]
                assert [record[0] for record in trace.records] == ["gitlab.publish_review"]
            else:
                assert tuple(row) == ("cancelled", "installation_unavailable")
                assert gitlab.calls == []
                assert trace.records == []
            assert [
                record for record in trace.records if record[0] == "github.publish_review"
            ] == []
            assert check_runs.calls == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())
