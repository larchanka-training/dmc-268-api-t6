"""Repository branch filter behind the ``RunSelectionRule`` port (SD §6.1, §7.3; PS T5)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.application.branch_patterns import branch_matches
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.infrastructure.models import CodeChange, Run


class SqlAlchemyBranchSelectionRule:
    """Match the PR target branch of a Run against ``repositories.branch_filter``.

    RunGuard calls it before the claim transaction, so it reads in its own short session.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def matches(self, run_id: UUID) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Run.base_ref, CodeChange.target_branch, Repository.branch_filter)
                    .join(CodeChange, Run.code_change_id == CodeChange.id)
                    .join(Repository, CodeChange.repository_id == Repository.id)
                    .where(Run.id == run_id)
                )
            ).one_or_none()
        if row is None:
            # A missing Run is RunGuard's decision, not the filter's.
            return True
        base_ref, target_branch, branch_filter = row
        return branch_matches(base_ref or target_branch, branch_filter)
