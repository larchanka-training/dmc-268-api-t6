"""Tests for workspace deletion (#550).

Covers authorization, name confirmation, balance forfeiture, and subscription cancellation.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.reviews_api import get_delete_workspace
from app.main import app
from app.modules.workspaces.application.delete_workspace import (
    DeleteWorkspace,
    DeleteWorkspaceResult,
    DeleteWorkspaceStore,
    DeleteWorkspaceUnitOfWork,
    NotWorkspaceOwner,
    PaymentGateway,
    WorkspaceNameMismatch,
    WorkspaceNotFound,
    WorkspaceRecord,
)
from app.modules.workspaces.domain.status import WorkspaceStatus
from app.modules.workspaces.infrastructure.delete_workspace_store import (
    SqlAlchemyDeleteWorkspaceStore,
)
from tests.portal_test_client import authenticated_test_client


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
        self.rolled_back = False

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
        self.rolled_back = True


class FakePaymentGateway(PaymentGateway):
    def __init__(
        self,
        uow: FakeDeleteWorkspaceUnitOfWork | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.cancelled_workspaces: list[UUID] = []
        self.calls: list[tuple[UUID, str, bool | None, bool]] = []
        self._uow = uow
        self._fail = fail

    async def cancel_subscription(
        self,
        workspace_id: UUID,
        *,
        idempotency_key: str,
        at_period_end: bool = False,
    ) -> None:
        self.cancelled_workspaces.append(workspace_id)
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


@pytest.fixture
def override_delete_workspace() -> Iterator[DeleteWorkspace]:
    installed: list[DeleteWorkspace] = []

    def _setter(use_case: DeleteWorkspace) -> None:
        installed.append(use_case)
        app.dependency_overrides[get_delete_workspace] = lambda: use_case

    yield _setter  # type: ignore[misc]
    app.dependency_overrides.pop(get_delete_workspace, None)


# ==============================================================================
# Use case unit tests
# ==============================================================================


@pytest.mark.anyio
async def test_delete_workspace_use_case_success_forfeits_balance_and_cancels_stripe() -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner_val=True,
        balance=Decimal("45.50"),
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)

    result = await use_case.execute(
        workspace_id=ws_id,
        confirmation_name="Acme Corp",
        user_id=42,
    )

    assert result == DeleteWorkspaceResult(workspace_id=ws_id, forfeited_balance=Decimal("45.50"))
    assert gateway.cancelled_workspaces == [ws_id]
    # Stripe cancellation must happen AFTER uow.commit() so no external HTTP runs inside DB tx.
    assert gateway.calls == [(ws_id, f"ws-delete-sub:{ws_id}", True, False)]
    assert store.forfeited == (ws_id, Decimal("45.50"))
    assert store.deleted == (ws_id, 42)
    assert uow.committed is True


@pytest.mark.anyio
async def test_sqlalchemy_store_is_owner_fails_closed_until_db_roles_land() -> None:
    from typing import Any, cast

    store = SqlAlchemyDeleteWorkspaceStore(cast(Any, None))
    assert await store.is_owner(uuid4(), 42) is False


@pytest.mark.anyio
async def test_delete_workspace_use_case_zero_balance_does_not_forfeit() -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(
            id=ws_id, name="Zero Balance Corp", status=WorkspaceStatus.ACTIVE
        ),
        is_owner_val=True,
        balance=Decimal("0.00"),
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)

    result = await use_case.execute(
        workspace_id=ws_id,
        confirmation_name="Zero Balance Corp",
        user_id=42,
    )

    assert result == DeleteWorkspaceResult(workspace_id=ws_id, forfeited_balance=Decimal("0"))
    assert store.forfeited is None
    assert store.deleted == (ws_id, 42)
    assert uow.committed is True


@pytest.mark.anyio
async def test_delete_workspace_use_case_not_found() -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(workspace=None)
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)

    with pytest.raises(WorkspaceNotFound):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme",
            user_id=42,
        )


@pytest.mark.anyio
async def test_delete_workspace_use_case_already_deleted() -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Deleted Corp", status=WorkspaceStatus.DELETED)
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)

    with pytest.raises(WorkspaceNotFound):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Deleted Corp",
            user_id=42,
        )


@pytest.mark.anyio
async def test_delete_workspace_use_case_non_owner_forbidden() -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme", status=WorkspaceStatus.ACTIVE),
        is_owner_val=False,
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)

    with pytest.raises(NotWorkspaceOwner):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme",
            user_id=42,
        )


@pytest.mark.anyio
async def test_delete_workspace_use_case_name_mismatch() -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE)
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)

    with pytest.raises(WorkspaceNameMismatch):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Wrong Corp",
            user_id=42,
        )


@pytest.mark.anyio
async def test_delete_workspace_stripe_gateway_failure_after_commit() -> None:
    """If Stripe gateway fails after commit, workspace remains committed deleted (ADR 0003 §5)."""
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner_val=True,
        balance=Decimal("45.50"),
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow, fail=True)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)

    with pytest.raises(RuntimeError, match="Stripe API network failure"):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme Corp",
            user_id=42,
        )

    # DB transaction was successfully committed before external Stripe call
    assert uow.committed is True
    assert store.deleted == (ws_id, 42)
    assert store.forfeited == (ws_id, Decimal("45.50"))
    assert gateway.cancelled_workspaces == [ws_id]


# ==============================================================================
# HTTP endpoint tests
# ==============================================================================


def test_delete_workspace_http_204_success(override_delete_workspace: object) -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner_val=True,
        balance=Decimal("25.00"),
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)
    override_delete_workspace(use_case)  # type: ignore[operator]

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme Corp"},
    )
    assert resp.status_code == 204
    assert store.deleted == (ws_id, 42)
    assert store.forfeited == (ws_id, Decimal("25.00"))


def test_delete_workspace_http_422_name_mismatch(override_delete_workspace: object) -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner_val=True,
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)
    override_delete_workspace(use_case)  # type: ignore[operator]

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Different Name"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "workspace_name_mismatch"


def test_delete_workspace_http_403_non_owner(override_delete_workspace: object) -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner_val=False,
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)
    override_delete_workspace(use_case)  # type: ignore[operator]

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme Corp"},
    )
    assert resp.status_code == 403
    assert "owner" in resp.json()["detail"].lower()


def test_delete_workspace_http_404_when_workspace_not_in_token_scope(
    override_delete_workspace: object,
) -> None:
    ws_id = uuid4()
    other_ws_id = uuid4()
    dummy_use_case = DeleteWorkspace(
        uow_factory=lambda: FakeDeleteWorkspaceUnitOfWork(FakeDeleteWorkspaceStore()),
        payment_gateway=FakePaymentGateway(),
    )
    override_delete_workspace(dummy_use_case)  # type: ignore[operator]

    client = authenticated_test_client(app, workspace_ids=[other_ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme Corp"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "workspace not found"


def test_delete_workspace_http_404_when_workspace_already_deleted(
    override_delete_workspace: object,
) -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.DELETED),
        is_owner_val=True,
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)
    override_delete_workspace(use_case)  # type: ignore[operator]

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme Corp"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "workspace not found"


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {},
        {"confirmation_name": ""},
        {"confirmation_name": "Acme", "extra": "field"},
    ],
)
def test_delete_workspace_http_422_invalid_payload(
    override_delete_workspace: object, invalid_payload: dict[str, object]
) -> None:
    ws_id = uuid4()
    dummy_use_case = DeleteWorkspace(
        uow_factory=lambda: FakeDeleteWorkspaceUnitOfWork(FakeDeleteWorkspaceStore()),
        payment_gateway=FakePaymentGateway(),
    )
    override_delete_workspace(dummy_use_case)  # type: ignore[operator]

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json=invalid_payload,
    )
    assert resp.status_code == 422


def test_delete_workspace_http_401_no_token() -> None:
    ws_id = uuid4()
    resp = TestClient(app).request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme"},
    )
    assert resp.status_code == 401
