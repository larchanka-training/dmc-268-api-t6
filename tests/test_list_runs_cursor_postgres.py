"""The run list cursor on PostgreSQL: runs that share created_at are ordered by id, none lost."""

from __future__ import annotations

import asyncio
import selectors
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.list_runs import ListRuns
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import PR_CLOSED, PR_OPEN, WS_A, Env

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration

_SAME = datetime(2026, 10, 27, 9, tzinfo=UTC)
_TIED = (
    UUID("00000000-0000-4000-8000-000000004563"),
    UUID("00000000-0000-4000-8000-000000004562"),
    UUID("00000000-0000-4000-8000-000000004561"),
)
_EARLIER = UUID("00000000-0000-4000-8000-000000004564")


def test_pages_of_one_walk_runs_with_one_created_at_by_id_without_gaps(portal_env: Env) -> None:
    async def exercise() -> list[UUID]:
        engine = portal_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET created_at = '2020-01-01T00:00:00Z'"),
                )
                for run_id, pr, created_at, key in (
                    (_TIED[1], PR_OPEN, _SAME, "n"),
                    (_TIED[0], PR_CLOSED, _SAME, "o"),
                    (_EARLIER, PR_OPEN, datetime(2026, 10, 27, 8, tzinfo=UTC), "p"),
                    (_TIED[2], PR_CLOSED, _SAME, "q"),
                ):
                    await session.execute(
                        text(
                            "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, "
                            "state, trigger, idempotency_key, engine, rule_version_id, "
                            "prompt_version_id, available_at, attempt, created_at, finished_at, "
                            "origin_provider_installation_id) "
                            "SELECT :id, :pr, base_sha, base_ref, head_sha, 'failed', 'manual', "
                            ":key, engine, rule_version_id, prompt_version_id, :created, 1, "
                            ":created, :created, origin_provider_installation_id FROM runs LIMIT 1"
                        ),
                        {"id": run_id, "pr": pr, "created": created_at, "key": key * 64},
                    )
            runs = ListRuns(SqlAlchemyRunRepository(factory, scope=AuthScope(42, (WS_A,))))
            walked: list[UUID] = []
            cursor: str | None = None
            for _ in range(10):
                page = await runs.execute(cursor=cursor, limit=1)
                walked.extend(item.id for item in page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
            return walked
        finally:
            await engine.dispose()

    walked = asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )

    # Newest first; a tie on created_at goes by id, descending, and the page after the
    # last tied run continues with the earlier one. Older seeded runs follow.
    assert walked[:4] == [*_TIED, _EARLIER]
    assert len(walked) == len(set(walked))
