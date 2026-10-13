"""Acquire review authority locks before the caller's Run row lock."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Result, Row, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.infrastructure.models import CodeChange, Run


async def lock_run_origin_row[RowT: tuple[Any, ...]](
    session: AsyncSession, run_id: UUID, statement: Select[RowT]
) -> Row[RowT] | None:
    """Keep rebind's installation -> Repository -> Run order in the caller's transaction."""
    await session.execute(
        select(ProviderInstallation.id)
        .join(Run, Run.origin_provider_installation_id == ProviderInstallation.id)
        .where(Run.id == run_id)
        .with_for_update(of=ProviderInstallation, read=True)
    )
    await session.execute(
        select(Repository.id)
        .join(CodeChange, CodeChange.repository_id == Repository.id)
        .join(Run, Run.code_change_id == CodeChange.id)
        .where(Run.id == run_id)
        .with_for_update(of=Repository, read=True)
    )
    result: Result[RowT] = await session.execute(statement.with_for_update(of=Run))
    return result.one_or_none()
