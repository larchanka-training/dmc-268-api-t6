"""``NOTIFY run_updated`` for a Run state change, in the caller's transaction (D12)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.enums import RunState
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore


async def notify_run_state(session: AsyncSession, run_id: UUID, status: RunState) -> None:
    workspace_id = await session.scalar(
        select(ProviderInstallation.workspace_id)
        .join(Repository, Repository.provider_installation_id == ProviderInstallation.id)
        .join(CodeChange, CodeChange.repository_id == Repository.id)
        .join(Run, Run.code_change_id == CodeChange.id)
        .where(Run.id == run_id)
    )
    if workspace_id is not None:
        await SqlAlchemyWebhookRunStore(session).notify_run_updated(
            run_id, workspace_id, status.value
        )
