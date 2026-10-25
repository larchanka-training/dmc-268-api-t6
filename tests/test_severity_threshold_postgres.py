"""Repository severity threshold on PostgreSQL (#149, PS §7, §9, §11)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.check_runs import CheckRunReport, check_run_view
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from tests.portal_postgres import HEAD, PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema

_PATH = "app/charge.py"
# New-version lines 10–13 lie inside the hunk, so every finding below is anchorable inline.
_PATCH = "@@ -10,3 +10,4 @@\n context\n+added\n context\n context"


@pytest.fixture
def threshold_env() -> Iterator[Env]:
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


async def _prepare(factory: async_sessionmaker[AsyncSession], threshold: str) -> None:
    """A running attempt of ``RUN_ATTEMPTED`` whose repository threshold changed after enqueue."""
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE repositories SET severity_threshold=:threshold WHERE id=:repo"),
            {"threshold": threshold, "repo": REPO_A},
        )
        await session.execute(
            text("UPDATE runs SET state='running', worker_id='worker' WHERE id=:run"),
            {"run": RUN_ATTEMPTED},
        )
        await session.execute(
            text(
                "INSERT INTO code_change_diffs (id, run_id, code_change_id, head_sha, filename, "
                "patch, additions, deletions) "
                "VALUES (:id, :run, :pr, :head, :path, :patch, :additions, :deletions)"
            ),
            {
                "id": uuid4(),
                "run": RUN_ATTEMPTED,
                "pr": PR_NEW,
                "head": HEAD,
                "path": _PATH,
                "patch": _PATCH,
                "additions": 1,
                "deletions": 0,
            },
        )


def _answer() -> dict[str, object]:
    def item(line: int, severity: str, title: str) -> dict[str, object]:
        return {
            "path": _PATH,
            "line": line,
            "start_line": None,
            "severity": severity,
            "category": "correctness",
            "title": title,
            "body": "The charge result is used without checking it.",
            "suggestion": None,
            "confidence": 0.9,
            "rule_name": None,
        }

    return {
        "findings": [
            item(11, "high", "Unchecked charge result"),
            item(12, "medium", "Retry has no backoff"),
            item(13, "low", "Loose status comparison"),
        ],
        "summary": {
            "problem": "A charge result is not checked.",
            "done_well": "The change is small.",
            "effort": "small",
        },
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("threshold", "drop_reasons", "below", "summary_line"),
    [
        ("all", [None, None, None], 0, None),
        (
            "critical",
            [None, "severity_threshold", "severity_threshold"],
            2,
            "ниже порога severity: 2",
        ),
    ],
    ids=["all", "critical"],
)
def test_findings_below_the_repository_threshold_are_stored_and_counted_on_the_check_run(
    threshold_env: Env,
    threshold: str,
    drop_reasons: list[str | None],
    below: int,
    summary_line: str | None,
) -> None:
    engine = threshold_env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    queue = RecordingQueue()

    async def scenario() -> tuple[list[tuple[object, ...]], str, CheckRunReport | None]:
        try:
            await _prepare(factory, threshold)
            stored = await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(RUN_ATTEMPTED, _answer())
            assert stored is True
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                await uow.reviews.mark_review_published(RUN_ATTEMPTED)
                await uow.commit()
            async with factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT title, drop_reason, inline_comment, published FROM findings "
                            "WHERE run_id=:run ORDER BY line_start"
                        ),
                        {"run": RUN_ATTEMPTED},
                    )
                ).all()
                body = (
                    await session.execute(
                        text("SELECT review_body FROM runs WHERE id=:run"), {"run": RUN_ATTEMPTED}
                    )
                ).scalar_one()
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            return [tuple(row) for row in rows], body, report
        finally:
            await engine.dispose()

    rows, body, report = asyncio.run(scenario())

    titles = ["Unchecked charge result", "Retry has no backoff", "Loose status comparison"]
    assert rows == [
        (title, reason, reason is None, reason is None)
        for title, reason in zip(titles, drop_reasons, strict=True)
    ]
    assert len(queue.pointers) == 1
    assert report is not None
    assert report.below_threshold_count == below
    view = check_run_view(report)
    if summary_line is None:
        assert "Below severity threshold" not in body
        assert view.title == "AI-ревью: blocking"
        assert view.summary == (
            "critical: 0, high: 1, medium: 1, low: 1, info: 0\ninline: 3, в теле ревью: 0"
        )
    else:
        assert f"**Below severity threshold ({threshold}):** {below}" in body
        assert view.title == "AI-ревью: blocking"
        assert view.summary == (
            "critical: 0, high: 1, medium: 0, low: 0, info: 0\n"
            f"inline: 1, в теле ревью: 0\n{summary_line}"
        )
