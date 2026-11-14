"""Delete a workspace permanently with balance forfeiture and subscription cancellation (#550)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.workspaces.domain.status import WorkspaceStatus


class WorkspaceNotFound(Exception):
    """The workspace does not exist, caller has no access, or it is already deleted."""


class NotWorkspaceOwner(Exception):
    """The caller does not hold the Owner role for this workspace."""


class WorkspaceNameMismatch(Exception):
    """The provided confirmation name does not match the workspace name exactly."""


@dataclass(frozen=True)
class WorkspaceRecord:
    id: UUID
    name: str
    status: WorkspaceStatus | str


@dataclass(frozen=True)
class DeleteWorkspaceResult:
    workspace_id: UUID
    forfeited_balance: Decimal


class PaymentGateway(Protocol):
    async def cancel_subscription(
        self,
        workspace_id: UUID,
        *,
        idempotency_key: str,
        at_period_end: bool = False,
    ) -> None: ...


class DeleteWorkspaceStore(Protocol):
    async def get_workspace_for_deletion(self, workspace_id: UUID) -> WorkspaceRecord | None: ...

    async def is_owner(self, workspace_id: UUID, user_id: int) -> bool: ...

    async def get_balance(self, workspace_id: UUID) -> Decimal: ...

    async def forfeit_balance(self, workspace_id: UUID, amount: Decimal) -> None: ...

    async def mark_deleted(self, workspace_id: UUID, user_id: int) -> None: ...


class DeleteWorkspaceUnitOfWork(UnitOfWork, Protocol):
    @property
    def store(self) -> DeleteWorkspaceStore: ...


class DeleteWorkspace:
    def __init__(
        self,
        uow_factory: Callable[[], DeleteWorkspaceUnitOfWork],
        payment_gateway: PaymentGateway,
    ) -> None:
        self._uow_factory = uow_factory
        self._payment_gateway = payment_gateway

    async def execute(
        self,
        *,
        workspace_id: UUID,
        confirmation_name: str,
        user_id: int,
    ) -> DeleteWorkspaceResult:
        async with self._uow_factory() as uow:
            workspace = await uow.store.get_workspace_for_deletion(workspace_id)
            if workspace is None or workspace.status == WorkspaceStatus.DELETED:
                raise WorkspaceNotFound(f"Workspace {workspace_id} not found")

            if not await uow.store.is_owner(workspace_id, user_id):
                raise NotWorkspaceOwner(f"User {user_id} is not owner of workspace {workspace_id}")

            if confirmation_name != workspace.name:
                raise WorkspaceNameMismatch(
                    f"Workspace name mismatch: expected '{workspace.name}', "
                    f"got '{confirmation_name}'"
                )

            current_balance = await uow.store.get_balance(workspace_id)
            forfeited = Decimal("0")
            if current_balance > Decimal("0"):
                await uow.store.forfeit_balance(workspace_id, current_balance)
                forfeited = current_balance

            await uow.store.mark_deleted(workspace_id, user_id)
            await uow.commit()

        # External Stripe cancellation MUST run outside any DB transaction (AGENTS.md, ADR 0003 §5).
        await self._payment_gateway.cancel_subscription(
            workspace_id,
            idempotency_key=f"ws-delete-sub:{workspace_id}",
            at_period_end=False,
        )

        return DeleteWorkspaceResult(workspace_id=workspace_id, forfeited_balance=forfeited)
