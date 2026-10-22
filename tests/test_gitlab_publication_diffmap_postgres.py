"""PostgreSQL: GitLab publication enrich path and redelivery placement (#171)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.publication_coordinates import gitlab_text_position
from app.modules.reviews.application.publish_run_review import PublishRunReview
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.infrastructure.gitlab_review_publication import (
    GitLabMergeRequestDiscussionGateway,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
)
from tests.portal_postgres import HEAD, INSTALL_A, PR_NEW, RUN_ATTEMPTED, Env, portal_schema
from tests.test_publish_run_review import Trace
from tests.test_run_origin_guard_postgres import prepare

# Portal run head; GitLab mock versions use the same head plus fixed base/start SHAs.
GIT_BASE = "1111111111111111111111111111111111111111"
GIT_START = "2222222222222222222222222222222222222222"

_SECTION_NINE_FIVE_PATCH = (
    "@@ -10,3 +10,4 @@\n before()\n-old_value()\n+new_value()\n+validate()\n after()"
)
_RANGE_LINE_CODE_SHA = "86f7e437faa5a7fce15d1ddcb9eaeaea377667b8"
DIFF_PATH = "a"

FINDING_CONTEXT_ID = UUID("00000000-0000-4000-8000-0000000000d1")
FINDING_RANGE_ID = UUID("00000000-0000-4000-8000-0000000000d2")
FINDING_INLINE_A_ID = UUID("00000000-0000-4000-8000-0000000000d3")
FINDING_INLINE_B_ID = UUID("00000000-0000-4000-8000-0000000000d4")

EXPECTED_CONTEXT_POSITION = {
    "position_type": "text",
    "base_sha": GIT_BASE,
    "start_sha": GIT_START,
    "head_sha": HEAD,
    "old_path": DIFF_PATH,
    "new_path": DIFF_PATH,
    "old_line": 12,
    "new_line": 13,
}

EXPECTED_RANGE_POSITION = {
    "position_type": "text",
    "base_sha": GIT_BASE,
    "start_sha": GIT_START,
    "head_sha": HEAD,
    "old_path": DIFF_PATH,
    "new_path": DIFF_PATH,
    "new_line": 12,
    "line_range": {
        "start": {
            "line_code": f"{_RANGE_LINE_CODE_SHA}_12_11",
            "type": "new",
            "new_line": 11,
        },
        "end": {
            "line_code": f"{_RANGE_LINE_CODE_SHA}_12_12",
            "type": "new",
            "new_line": 12,
        },
    },
}


@pytest.fixture
def gitlab_publication_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def _gitlab_publishing_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    finding_rows: list[tuple[UUID, str, int, int | None, str, str]],
) -> None:
    """``publishing`` GitLab run with §9.5 diff and inline findings (no manual gitlab_*)."""
    await prepare(factory, "active", "publishing")
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE provider_installations SET provider='gitlab' WHERE id=:id"),
            {"id": INSTALL_A},
        )
        await session.execute(
            text("UPDATE runs SET review_body=:body, cancel_requested=false WHERE id=:id"),
            {"id": RUN_ATTEMPTED, "body": "## Review summary\n\n**Verdict:** blocking"},
        )
        await session.execute(
            text(
                "INSERT INTO code_change_diffs "
                "(id, run_id, code_change_id, head_sha, filename, patch, additions, deletions) "
                "VALUES (:id, :run, :pr, :head, :path, :patch, :additions, :deletions)"
            ),
            {
                "id": uuid4(),
                "run": RUN_ATTEMPTED,
                "pr": PR_NEW,
                "head": HEAD,
                "path": DIFF_PATH,
                "patch": _SECTION_NINE_FIVE_PATCH,
                "additions": 2,
                "deletions": 1,
            },
        )
        for finding_id, path, line_start, line_end, title, body in finding_rows:
            await session.execute(
                text(
                    "INSERT INTO findings "
                    "(id, run_id, file_path, line_start, line_end, severity, confidence, "
                    "category, title, body, published, inline_comment) VALUES "
                    "(:id, :run, :path, :start, :end, 'high', 0.9, 'correctness', :title, :body, "
                    "false, true)"
                ),
                {
                    "id": finding_id,
                    "run": RUN_ATTEMPTED,
                    "path": path,
                    "start": line_start,
                    "end": line_end,
                    "title": title,
                    "body": body,
                },
            )


def _gitlab_versions() -> list[dict[str, str]]:
    return [
        {
            "head_commit_sha": HEAD,
            "base_commit_sha": GIT_BASE,
            "start_commit_sha": GIT_START,
        }
    ]


def _json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content) if request.content else {}


def _fail_once_uow_factory(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fail_complete: list[bool],
) -> Any:
    """UoW factory that raises once inside ``complete_publication`` (before commit)."""

    @asynccontextmanager
    async def _factory() -> AsyncIterator[SqlAlchemyRunLifecycleUnitOfWork]:
        async with SqlAlchemyRunLifecycleUnitOfWork(session_factory) as uow:
            original = uow.runs.complete_publication

            async def complete_publication(*args: Any, **kwargs: Any) -> str | None:
                if fail_complete and fail_complete[0]:
                    fail_complete[0] = False
                    raise RuntimeError("simulated crash before publication commit")
                return await original(*args, **kwargs)

            uow.runs.complete_publication = complete_publication  # type: ignore[method-assign]
            yield uow

    return _factory


@pytest.mark.integration
@pytest.mark.parametrize(
    ("finding_id", "line_start", "line_end", "title", "expected_position"),
    [
        (
            FINDING_CONTEXT_ID,
            13,
            None,
            "Trailing context",
            EXPECTED_CONTEXT_POSITION,
        ),
        (
            FINDING_RANGE_ID,
            11,
            12,
            "Validation runs after change",
            EXPECTED_RANGE_POSITION,
        ),
    ],
)
def test_gitlab_publish_posts_positions_built_from_stored_diff(
    gitlab_publication_env: Env,
    finding_id: UUID,
    line_start: int,
    line_end: int | None,
    title: str,
    expected_position: dict[str, Any],
) -> None:
    """DB diff + findings → publish_context enrich → GitLab POST ``position`` (§9.5 literals)."""

    async def exercise() -> None:
        engine = gitlab_publication_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        posts: list[dict[str, Any]] = []
        prefix = "/api/v4/projects/101/merge_requests/3"

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == f"{prefix}/versions":
                return httpx.Response(200, json=_gitlab_versions())
            if request.method == "GET" and request.url.path == f"{prefix}/discussions":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == f"{prefix}/discussions":
                payload = _json_body(request)
                posts.append(payload)
                note_id = 701 if "position" in payload else 880
                return httpx.Response(
                    201, json={"id": "disc", "notes": [{"id": note_id, "body": ""}]}
                )
            raise AssertionError(f"unexpected {request.method} {request.url}")

        try:
            await _gitlab_publishing_run(
                factory,
                finding_rows=[
                    (finding_id, DIFF_PATH, line_start, line_end, title, "Body text."),
                ],
            )
            async with factory() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(RUN_ATTEMPTED)
            assert context is not None
            assert len(context.findings) == 1
            enriched = context.findings[0]
            assert enriched.finding_id == finding_id
            version = {
                "base_commit_sha": GIT_BASE,
                "start_commit_sha": GIT_START,
                "head_commit_sha": HEAD,
            }
            assert gitlab_text_position(version, enriched) == expected_position

            gateway = GitLabMergeRequestDiscussionGateway(
                client=httpx.AsyncClient(
                    base_url="https://gitlab.example.test",
                    transport=httpx.MockTransport(respond),
                ),
                token="glpat-test",
            )
            trace = Trace()
            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_reviews=gateway,
            ).execute(ReviewPublishPointer(RUN_ATTEMPTED, HEAD, context.findings_hash, "COMMENT"))
            inline_posts = [item for item in posts if "position" in item]
            assert len(inline_posts) == 1
            assert inline_posts[0]["position"] == expected_position
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_gitlab_redelivery_after_failed_complete_restores_inline_placement(
    gitlab_publication_env: Env,
) -> None:
    """Partial coordinates fallback + crash before commit; redelivery fixes ``inline_comment``."""

    async def exercise() -> None:
        engine = gitlab_publication_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        discussions: list[dict[str, Any]] = []
        inline_attempts = 0
        prefix = "/api/v4/projects/101/merge_requests/3"
        fail_complete = [True]

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal inline_attempts
            if request.method == "GET" and request.url.path == f"{prefix}/versions":
                return httpx.Response(200, json=_gitlab_versions())
            if request.method == "GET" and request.url.path == f"{prefix}/discussions":
                return httpx.Response(200, json=discussions)
            if request.method == "POST" and request.url.path == f"{prefix}/discussions":
                payload = _json_body(request)
                if "position" in payload:
                    inline_attempts += 1
                    if inline_attempts == 2:
                        return httpx.Response(
                            422,
                            json={"message": "line_code is invalid for this diff version"},
                        )
                    note_id = 610 + inline_attempts
                    discussions.append(
                        {
                            "id": f"inline-{inline_attempts}",
                            "notes": [{"id": note_id, "body": payload["body"]}],
                        }
                    )
                    return httpx.Response(
                        201,
                        json={"id": "disc", "notes": [{"id": note_id, "body": ""}]},
                    )
                discussions.append(
                    {
                        "id": "summary",
                        "notes": [{"id": 880, "body": payload["body"]}],
                    }
                )
                return httpx.Response(201, json={"id": "disc", "notes": [{"id": 880, "body": ""}]})
            raise AssertionError(f"unexpected {request.method} {request.url}")

        gateway = GitLabMergeRequestDiscussionGateway(
            client=httpx.AsyncClient(
                base_url="https://gitlab.example.test",
                transport=httpx.MockTransport(respond),
            ),
            token="glpat-test",
        )
        trace = Trace()

        try:
            await _gitlab_publishing_run(
                factory,
                finding_rows=[
                    (
                        FINDING_INLINE_A_ID,
                        DIFF_PATH,
                        11,
                        None,
                        "First inline",
                        "First body.",
                    ),
                    (
                        FINDING_INLINE_B_ID,
                        DIFF_PATH,
                        12,
                        None,
                        "Second inline",
                        "Second body.",
                    ),
                ],
            )
            async with factory() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(RUN_ATTEMPTED)
            assert context is not None
            pointer = ReviewPublishPointer(RUN_ATTEMPTED, HEAD, context.findings_hash, "COMMENT")
            publisher = PublishRunReview(
                uow_factory=_fail_once_uow_factory(factory, fail_complete=fail_complete),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_reviews=gateway,
            )
            with pytest.raises(RuntimeError, match="simulated crash"):
                await publisher.execute(pointer)

            async with factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, inline_comment FROM findings WHERE run_id=:run "
                            "ORDER BY line_start"
                        ),
                        {"run": RUN_ATTEMPTED},
                    )
                ).tuples().all()
                run_state = await session.scalar(
                    text("SELECT state::text FROM runs WHERE id=:id"),
                    {"id": RUN_ATTEMPTED},
                )
            assert run_state == "publishing"
            assert rows == [(FINDING_INLINE_A_ID, True), (FINDING_INLINE_B_ID, True)]

            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_reviews=gateway,
            ).execute(pointer)

            async with factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, inline_comment FROM findings WHERE run_id=:run "
                            "ORDER BY line_start"
                        ),
                        {"run": RUN_ATTEMPTED},
                    )
                ).tuples().all()
                run_state = await session.scalar(
                    text("SELECT state::text FROM runs WHERE id=:id"),
                    {"id": RUN_ATTEMPTED},
                )
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            assert run_state == "succeeded"
            assert rows == [(FINDING_INLINE_A_ID, True), (FINDING_INLINE_B_ID, False)]
            assert report is not None
            assert report.inline_count == 1
            assert report.body_count == 1
            assert inline_attempts == 2
        finally:
            await engine.dispose()

    asyncio.run(exercise())
