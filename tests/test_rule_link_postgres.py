"""The rules link of a stored finding and the redelivery of a review that carries it (#465)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application.findings_post_processor import (
    ProcessedFinding,
    ProcessedReviewOutput,
)
from app.modules.reviews.application.publish_run_review import PublishRunReview
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import (
    ReviewFinding,
    ReviewOutput,
    ReviewSummary,
)
from app.modules.reviews.application.run_coverage import CoverageItem, CoverageStatus, RunCoverage
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.store_review_output import hashed_findings
from app.modules.reviews.application.verdict import findings_hash
from app.modules.reviews.infrastructure.github_review_publication import (
    GitHubPullRequestReviewGateway,
)
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import HEAD, INSTALL_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_retired_installation_postgres import DiscardTokenCache

_LINK = (
    "https://github.com/octo/a/blob/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "/.review/rules.md?plain=1#L5-L8"
)
_LINKED = UUID(int=46501)
_PLAIN = UUID(int=46502)
_SUMMARY = ReviewSummary(problem="A bug.", done_well="Small change.", effort="small")


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


@pytest.mark.integration
def test_the_hash_of_the_store_matches_the_hash_of_the_stored_rows(env: Env) -> None:
    finding = ReviewFinding(
        path="a.py",
        line=3,
        start_line=None,
        severity="medium",
        category="correctness",
        title="Rule",
        body="Unsanitized input.",
        suggestion=None,
        confidence=0.8,
        rule_name=".review/rules.md",
    )
    processed = ProcessedReviewOutput(
        inline=(ProcessedFinding(finding, 0, "inline", rule_link=_LINK),),
        body_only=(),
        dropped=(),
        review_body="A bug.",
        coverage=RunCoverage(
            CoverageStatus.PARTIAL,
            0,
            reasons=(CoverageItem(None, "classification_unavailable"),),
        ),
    )

    async def exercise() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                stored = await uow.reviews.store_review_output(
                    RUN_ATTEMPTED,
                    {"findings": [], "summary": _SUMMARY.model_dump()},
                    ReviewOutput(findings=[finding], summary=_SUMMARY),
                    processed,
                )
                assert stored is not None
                await uow.commit()
            async with factory() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(RUN_ATTEMPTED)
            assert context is not None
            assert [item.rule_link for item in context.findings] == [_LINK]
            # The pointer of T8 carries the first hash, the publisher compares it to the second.
            assert context.findings_hash == findings_hash(hashed_findings(processed))
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_a_redelivered_review_keeps_its_findings_inline_with_their_comments(env: Env) -> None:
    """T17: a review found by its marker recognizes both a comment with a link and one without."""
    comments = [
        {
            "id": 901,
            "path": "a.py",
            "line": 3,
            "body": f"**Rule**\n\nB\n\nRule: [.review/rules.md#L5-L8]({_LINK})",
        },
        {"id": 902, "path": "a.py", "line": 4, "body": "**Plain**\n\nB"},
    ]

    async def exercise() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        marker = ""

        async def http(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/app/installations/17/access_tokens":
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if request.method == "GET" and path.endswith("/pulls/3/reviews"):
                return httpx.Response(
                    200, json=[{"id": 55, "body": f"<!-- ai-review findings_hash={marker} -->"}]
                )
            if request.method == "GET" and path.endswith("/reviews/55/comments"):
                return httpx.Response(200, json=comments)
            raise AssertionError(f"unexpected {request.method} {path}")

        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO findings (id,run_id,file_path,line_start,severity,confidence,"
                        "category,title,body,rule_name,rule_link,published,inline_comment) VALUES "
                        "(:linked,:run,'a.py',3,'medium',0.8,'correctness','Rule','B',"
                        "'.review/rules.md',:link,false,true),"
                        "(:plain,:run,'a.py',4,'medium',0.8,'correctness','Plain','B',"
                        "NULL,NULL,false,true)"
                    ),
                    {"linked": _LINKED, "plain": _PLAIN, "run": RUN_ATTEMPTED, "link": _LINK},
                )
                await session.execute(
                    text(
                        "UPDATE runs SET state='publishing',origin_provider_installation_id=:origin"
                        " WHERE id=:id"
                    ),
                    {"origin": INSTALL_A, "id": RUN_ATTEMPTED},
                )
            async with factory() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(RUN_ATTEMPTED)
            assert context is not None
            marker = context.findings_hash
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
                    reviews=GitHubPullRequestReviewGateway(client=client, token_provider=tokens),
                    check_runs=None,
                    trace=TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory)),
                ).execute(ReviewPublishPointer(RUN_ATTEMPTED, HEAD, marker, "COMMENT"))
            async with factory() as session:
                rows = await session.execute(
                    text(
                        "SELECT f.id,f.inline_comment,c.github_comment_id FROM findings f "
                        "LEFT JOIN comments c ON c.finding_id=f.id WHERE f.run_id=:run"
                    ),
                    {"run": RUN_ATTEMPTED},
                )
                state = await session.scalar(
                    text("SELECT state::text FROM runs WHERE id=:id"), {"id": RUN_ATTEMPTED}
                )
            assert {tuple(row) for row in rows} == {(_LINKED, True, 901), (_PLAIN, True, 902)}
            assert state == "succeeded"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("provider", "expected"), [("github", "octo/a"), ("gitlab", None), ("bitbucket", None)]
)
def test_only_a_github_repository_is_named_for_the_link(
    env: Env, provider: str, expected: str | None
) -> None:
    async def exercise() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET provider=:provider WHERE id=:id"),
                    {"provider": provider, "id": INSTALL_A},
                )
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                context = await uow.reviews.get_post_processing_input(RUN_ATTEMPTED)
            assert context is not None
            assert context.github_repository_full_name == expected
        finally:
            await engine.dispose()

    asyncio.run(exercise())
