"""SQLAlchemy implementation of workspace deletion persistence contract (#550)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import LedgerKind
from app.modules.billing.infrastructure.models import CreditLedger
from app.modules.workspaces.application.delete_workspace import (
    WorkspaceRecord,
)
from app.modules.workspaces.domain.status import WorkspaceStatus
from app.modules.workspaces.infrastructure.models import (
    GitHubUserWorkspaceAccess,
    Workspace,
)


class SqlAlchemyDeleteWorkspaceStore:
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
        return WorkspaceRecord(id=row[0], name=row[1], status=row[2])

    async def is_owner(self, workspace_id: UUID, user_id: int) -> bool:
        """Fail closed until DB-backed Owner roles (#373, ADR 0002 §11.2, ADR 0003 §3.1) land."""
        del workspace_id, user_id
        return False

    async def get_balance(self, workspace_id: UUID) -> Decimal:
        balance = await self._session.scalar(
            select(func.coalesce(func.sum(CreditLedger.amount_usd), Decimal("0"))).where(
                CreditLedger.workspace_id == workspace_id
            )
        )
        return Decimal(str(balance or 0))

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
        await self._session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                status=WorkspaceStatus.DELETED,
                deleted_at=func.clock_timestamp(),
                deleted_by=user_id,
            )
        )
        await self._session.execute(
            delete(GitHubUserWorkspaceAccess).where(
                GitHubUserWorkspaceAccess.workspace_id == workspace_id
            )
        )


class SqlAlchemyDeleteWorkspaceUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._store: SqlAlchemyDeleteWorkspaceStore | None = None

    async def __aenter__(self) -> SqlAlchemyDeleteWorkspaceUnitOfWork:
        self._session = self._session_factory()
        await self._session.__aenter__()
        self._store = SqlAlchemyDeleteWorkspaceStore(self._session)
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._session is not None:
            await self._session.__aexit__(exc_type, exc_val, exc_tb)
            self._session = None
            self._store = None

    @property
    def store(self) -> SqlAlchemyDeleteWorkspaceStore:
        if self._store is None:
            raise RuntimeError("DeleteWorkspaceUnitOfWork is not open")
        return self._store

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("DeleteWorkspaceUnitOfWork is not open")
        await self._session.commit()

    async def rollback(self) -> None:
        if self._session is None:
            raise RuntimeError("DeleteWorkspaceUnitOfWork is not open")
        await self._session.rollback()
