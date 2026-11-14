"""SQLAlchemy implementation of workspace deletion persistence contract (#550)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.enums import LedgerKind
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.billing.infrastructure.models import CreditLedger
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.workspaces.application.delete_workspace import (
    DeleteWorkspaceStore,
    DeleteWorkspaceUnitOfWork,
    WorkspaceRecord,
)
from app.modules.workspaces.domain.status import WorkspaceStatus
from app.modules.workspaces.infrastructure.models import (
    GitHubUserWorkspaceAccess,
    Workspace,
)


class SqlAlchemyDeleteWorkspaceStore(DeleteWorkspaceStore):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_workspace_for_deletion(self, workspace_id: UUID) -> WorkspaceRecord | None:
        row = (
            await self._session.execute(
                select(Workspace.id, Workspace.name, Workspace.status)
                .where(Workspace.id == workspace_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            return None
        return WorkspaceRecord(id=row.id, name=row.name, status=row.status)

    async def is_owner(self, workspace_id: UUID, user_id: int) -> bool:
        del workspace_id, user_id
        # Stub: role verification deferred to #373 (#545 scope agreement with tech lead)
        return False

    async def get_balance(self, workspace_id: UUID) -> Decimal:
        balance = await self._session.scalar(
            select(func.coalesce(func.sum(CreditLedger.amount_usd), Decimal("0"))).where(
                CreditLedger.workspace_id == workspace_id
            )
        )
        return Decimal("0") if balance is None else Decimal(str(balance))

    async def forfeit_balance(self, workspace_id: UUID, amount: Decimal) -> None:
        entry = CreditLedger(
            id=uuid4(),
            workspace_id=workspace_id,
            kind=LedgerKind.ADJUSTMENT,
            amount_usd=-amount,
            reason="workspace_deleted",
        )
        self._session.add(entry)

    async def mark_deleted(self, workspace_id: UUID, user_id: int) -> None:
        now = func.clock_timestamp()
        await self._session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                status=WorkspaceStatus.DELETED,
                deleted_at=now,
                deleted_by=user_id,
            )
        )
        await self._session.execute(
            delete(GitHubUserWorkspaceAccess).where(
                GitHubUserWorkspaceAccess.workspace_id == workspace_id
            )
        )
        await self._session.execute(
            update(ProviderInstallation)
            .where(
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )


class SqlAlchemyDeleteWorkspaceUnitOfWork(SqlAlchemyUnitOfWork, DeleteWorkspaceUnitOfWork):
    @property
    def store(self) -> DeleteWorkspaceStore:
        return SqlAlchemyDeleteWorkspaceStore(self.session)
