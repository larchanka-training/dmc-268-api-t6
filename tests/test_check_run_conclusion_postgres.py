"""Check-run conclusion of a succeeded Run follows the published review event (#320, PS §7)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.check_runs import check_run_view
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import HEAD, PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema

_PATH = "app/charge.py"
_PATCH = "@@ -10,3 +10,4 @@\n context\n+added\n context\n context"


@pytest.fixture
def conclusion_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


@dataclass
class RecordingQueue:
    pointers: list[ReviewPublishPointer] = field(default_factory=list)

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        self.pointers.append(pointer)


async def _prepare(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET state='running', worker_id='worker' WHERE id=:run"),
            {"run": RUN_ATTEMPTED},
        )
        await session.execute(
            text(
                "INSERT INTO code_change_diffs (id, run_id, code_change_id, head_sha, filename, "
                "patch, additions, deletions) "
                "VALUES (:id, :run, :pr, :head, :path, :patch, 1, 0)"
            ),
            {
                "id": uuid4(),
                "run": RUN_ATTEMPTED,
                "pr": PR_NEW,
                "head": HEAD,
                "path": _PATH,
                "patch": _PATCH,
            },
        )


def _blocking_answer() -> dict[str, object]:
    return {
        "findings": [
            {
                "path": _PATH,
                "line": 11,
                "start_line": None,
                "severity": "high",
                "category": "correctness",
                "title": "Unchecked charge result",
                "body": "The charge result is used without checking it.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {
            "problem": "A charge result is not checked.",
            "done_well": "The change is small.",
            "effort": "small",
        },
    }


def _publication(event: str, response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    request = {
        "head_sha": HEAD,
        "findings_hash": "f" * 64,
        "review_event": event,
        "inline_count": 1,
        "try": 1,
    }
    return request, response


@pytest.mark.integration
@pytest.mark.parametrize(
    ("published", "setting_now", "conclusion"),
    [
        ([_publication("REQUEST_CHANGES", {"github_review_id": 501})], "comment", "failure"),
        ([_publication("COMMENT", {"github_review_id": 501})], "request_changes", "neutral"),
        (
            [
                _publication("REQUEST_CHANGES", {"error": {"http_status": 502}}),
                _publication("REQUEST_CHANGES", {"github_review_id": 501}),
            ],
            "comment",
            "failure",
        ),
        ([_publication("REQUEST_CHANGES", {"error": {"http_status": 502}})], "comment", "neutral"),
        ([], "request_changes", "neutral"),
        (
            # T17 after a lost lease: the posted review is found by its marker under the
            # rebuilt pointer; GitHub keeps the event of the first accepted POST.
            [
                _publication("REQUEST_CHANGES", {"github_review_id": 501}),
                _publication("COMMENT", {"github_review_id": 501}),
            ],
            "comment",
            "failure",
        ),
        (
            [
                _publication("COMMENT", {"github_review_id": 501}),
                _publication("REQUEST_CHANGES", {"github_review_id": 501}),
            ],
            "request_changes",
            "neutral",
        ),
    ],
    ids=[
        "request-changes-then-setting-changed",
        "comment-then-setting-changed",
        "request-changes-after-retry",
        "request-changes-never-accepted",
        "no-publication",
        "request-changes-then-found-as-comment",
        "comment-then-found-as-request-changes",
    ],
)
def test_succeeded_blocking_run_concludes_from_the_published_event_not_the_setting(
    conclusion_env: Env,
    published: list[tuple[dict[str, Any], dict[str, Any]]],
    setting_now: str,
    conclusion: str,
) -> None:
    engine = conclusion_env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))

    async def scenario() -> tuple[str | None, str | None]:
        try:
            await _prepare(factory)
            stored = await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                RecordingQueue(),
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(RUN_ATTEMPTED, _blocking_answer())
            assert stored is True
            for request, response in published:
                await trace.record(
                    RUN_ATTEMPTED,
                    "github.publish_review",
                    request,
                    response,
                    datetime.now(UTC),
                    5,
                )
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                await uow.reviews.mark_review_published(RUN_ATTEMPTED)
                await uow.commit()
            # A later RunGuard delivery re-renders the check-run after the setting changed.
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET review_event=:event WHERE id=:repo"),
                    {"event": setting_now, "repo": REPO_A},
                )
            async with factory() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            assert report is not None
            return report.verdict, check_run_view(report).conclusion
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == ("blocking", conclusion)
