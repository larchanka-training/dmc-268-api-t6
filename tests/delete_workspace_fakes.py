"""Test fakes for workspace deletion tests."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from app.modules.workspaces.application.delete_workspace import (
    DeleteWorkspaceStore,
    DeleteWorkspaceUnitOfWork,
    PaymentGateway,
    WorkspaceRecord,
)
from app.modules.workspaces.domain.status import WorkspaceStatus


class FakeDeleteWorkspaceStore(DeleteWorkspaceStore):
    def __init__(
        self,
        workspace: WorkspaceRecord | None = None,
        is_owner_val: bool = True,
        balance: Decimal = Decimal("50.00"),
    ) -> None:
        self.workspace = workspace
        self.is_owner_val = is_owner_val
        self.balance = balance
        self.forfeited: tuple[UUID, Decimal] | None = None
        self.deleted: tuple[UUID, int] | None = None

    async def get_workspace_for_deletion(self, workspace_id: UUID) -> WorkspaceRecord | None:
        if self.workspace and self.workspace.id == workspace_id:
            return self.workspace
        return None

    async def is_owner(self, workspace_id: UUID, user_id: int) -> bool:
        return self.is_owner_val

    async def get_balance(self, workspace_id: UUID) -> Decimal:
        return self.balance

    async def forfeit_balance(self, workspace_id: UUID, amount: Decimal) -> None:
        self.forfeited = (workspace_id, amount)

    async def mark_deleted(self, workspace_id: UUID, user_id: int) -> None:
        self.deleted = (workspace_id, user_id)
        if self.workspace:
            self.workspace = WorkspaceRecord(
                id=self.workspace.id,
                name=self.workspace.name,
                status=WorkspaceStatus.DELETED,
            )


class FakeDeleteWorkspaceUnitOfWork(DeleteWorkspaceUnitOfWork):
    def __init__(self, store: FakeDeleteWorkspaceStore) -> None:
        self._store = store
        self.committed = False

    async def __aenter__(self) -> FakeDeleteWorkspaceUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        pass

    @property
    def store(self) -> DeleteWorkspaceStore:
        return self._store

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        pass


class FakePaymentGateway(PaymentGateway):
    def __init__(
        self,
        uow: FakeDeleteWorkspaceUnitOfWork | None = None,
        *,
        fail: bool = False,
        unconfigured: bool = False,
    ) -> None:
        self.calls: list[tuple[UUID, str, bool | None, bool]] = []
        self._uow = uow
        self._fail = fail
        self._unconfigured = unconfigured

    def ensure_configured(self) -> None:
        if self._unconfigured:
            raise NotImplementedError("Payment gateway unconfigured")

    async def cancel_subscription(
        self,
        workspace_id: UUID,
        *,
        idempotency_key: str,
        at_period_end: bool = False,
    ) -> None:
        self.calls.append(
            (
                workspace_id,
                idempotency_key,
                self._uow.committed if self._uow else None,
                at_period_end,
            )
        )
        if self._fail:
            raise RuntimeError("Stripe API network failure")
