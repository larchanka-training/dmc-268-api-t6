"""Shared PostgreSQL helper of the publication settings tests (#212, #387).

``_settings`` writes repository A's live publication pair, the one a new run pins at its
creation; the run publication and snapshot path tests change it between the steps of a run.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.portal_postgres import REPO_A


async def _settings(factory: async_sessionmaker[AsyncSession], threshold: str, cap: int) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE repositories SET severity_threshold=:threshold, "
                "max_comments=:cap WHERE id=:repo"
            ),
            {"threshold": threshold, "cap": cap, "repo": REPO_A},
        )
