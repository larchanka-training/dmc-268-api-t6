"""Migration 0026 accepts every provider diff status in persisted snapshots."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.reviews.infrastructure.models import CodeChangeDiff
from tests.portal_postgres import HEAD, PR_OPEN, RUN_DONE, Env, portal_schema


@pytest.fixture
def env() -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test persisted diff statuses")
    with portal_schema(url, None) as schema:
        yield schema


@pytest.mark.integration
def test_diff_snapshots_persist_copied_changed_and_unchanged(env: Env) -> None:
    async def scenario() -> None:
        engine = env.engine()
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as session:
                session.add_all(
                    [
                        CodeChangeDiff(
                            run_id=RUN_DONE,
                            code_change_id=PR_OPEN,
                            head_sha=HEAD,
                            filename=f"{status}.py",
                            status=status,
                            patch=None,
                        )
                        for status in ("copied", "changed", "unchanged")
                    ]
                )
            async with factory() as session:
                rows = (
                    await session.execute(
                        select(CodeChangeDiff.filename, CodeChangeDiff.status)
                        .where(CodeChangeDiff.run_id == RUN_DONE)
                        .order_by(CodeChangeDiff.filename)
                    )
                ).all()
            assert [tuple(row) for row in rows] == [
                ("changed.py", "changed"),
                ("copied.py", "copied"),
                ("unchanged.py", "unchanged"),
            ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
