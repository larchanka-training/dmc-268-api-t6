"""Tests for workspace deletion (#550).

Covers authorization, name confirmation, balance forfeiture, and subscription cancellation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.reviews_api import get_delete_workspace
from app.main import app
from app.modules.workspaces.application.delete_workspace import (
    DeleteWorkspace,
    DeleteWorkspaceResult,
    NotWorkspaceOwner,
    WorkspaceNameMismatch,
    WorkspaceNotFound,
    WorkspaceRecord,
)
from app.modules.workspaces.domain.status import WorkspaceStatus
from app.modules.workspaces.infrastructure.delete_workspace_store import (
    SqlAlchemyDeleteWorkspaceStore,
)
from tests.delete_workspace_fakes import (
    FakeDeleteWorkspaceStore,
    FakeDeleteWorkspaceUnitOfWork,
    FakePaymentGateway,
)
from tests.portal_test_client import authenticated_test_client


def _make_use_case(
    workspace: WorkspaceRecord | None = None,
    *,
    is_owner: bool = True,
    balance: Decimal = Decimal("50.00"),
    gateway_fail: bool = False,
    gateway_unconfigured: bool = False,
) -> tuple[
    DeleteWorkspace, FakeDeleteWorkspaceStore, FakeDeleteWorkspaceUnitOfWork, FakePaymentGateway
]:
    store = FakeDeleteWorkspaceStore(workspace=workspace, is_owner_val=is_owner, balance=balance)
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    gateway = FakePaymentGateway(uow, fail=gateway_fail, unconfigured=gateway_unconfigured)
    use_case = DeleteWorkspace(uow_factory=lambda: uow, payment_gateway=gateway)
    return use_case, store, uow, gateway


@pytest.fixture
def override_delete_workspace() -> Iterator[Callable[[DeleteWorkspace], None]]:
    def _setter(use_case: DeleteWorkspace) -> None:
        app.dependency_overrides[get_delete_workspace] = lambda: use_case

    yield _setter
    app.dependency_overrides.pop(get_delete_workspace, None)


# ==============================================================================
# Use case unit tests
# ==============================================================================


@pytest.mark.anyio
async def test_delete_workspace_use_case_success_forfeits_balance_and_cancels_stripe() -> None:
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner=True,
        balance=Decimal("45.50"),
    )

    result = await use_case.execute(
        workspace_id=ws_id,
        confirmation_name="Acme Corp",
        user_id=42,
    )

    assert result == DeleteWorkspaceResult(workspace_id=ws_id, forfeited_balance=Decimal("45.50"))
    assert [c[0] for c in gateway.calls] == [ws_id]
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
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(
            id=ws_id, name="Zero Balance Corp", status=WorkspaceStatus.ACTIVE
        ),
        is_owner=True,
        balance=Decimal("0.00"),
    )

    result = await use_case.execute(
        workspace_id=ws_id,
        confirmation_name="Zero Balance Corp",
        user_id=42,
    )

    assert result == DeleteWorkspaceResult(workspace_id=ws_id, forfeited_balance=Decimal("0"))
    assert store.forfeited is None
    assert store.deleted == (ws_id, 42)
    assert uow.committed is True
    assert gateway.calls == [(ws_id, f"ws-delete-sub:{ws_id}", True, False)]


@pytest.mark.anyio
async def test_delete_workspace_use_case_not_found() -> None:
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(workspace=None)

    with pytest.raises(WorkspaceNotFound):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme",
            user_id=42,
        )
    assert uow.committed is False
    assert store.deleted is None
    assert store.forfeited is None
    assert gateway.calls == []


@pytest.mark.anyio
async def test_delete_workspace_use_case_already_deleted() -> None:
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Deleted Corp", status=WorkspaceStatus.DELETED)
    )

    with pytest.raises(WorkspaceNotFound):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Deleted Corp",
            user_id=42,
        )
    assert uow.committed is False
    assert store.deleted is None
    assert store.forfeited is None
    assert gateway.calls == []


@pytest.mark.anyio
async def test_delete_workspace_use_case_non_owner_forbidden() -> None:
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme", status=WorkspaceStatus.ACTIVE),
        is_owner=False,
    )

    with pytest.raises(NotWorkspaceOwner):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme",
            user_id=42,
        )
    assert uow.committed is False
    assert store.deleted is None
    assert store.forfeited is None
    assert gateway.calls == []


@pytest.mark.anyio
async def test_delete_workspace_use_case_name_mismatch() -> None:
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE)
    )

    with pytest.raises(WorkspaceNameMismatch):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Wrong Corp",
            user_id=42,
        )
    assert uow.committed is False
    assert store.deleted is None
    assert store.forfeited is None
    assert gateway.calls == []


@pytest.mark.anyio
async def test_delete_workspace_fails_before_commit_when_gateway_unconfigured() -> None:
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        gateway_unconfigured=True,
    )

    with pytest.raises(NotImplementedError, match="Payment gateway unconfigured"):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme Corp",
            user_id=42,
        )
    assert uow.committed is False
    assert store.deleted is None
    assert store.forfeited is None
    assert gateway.calls == []


@pytest.mark.anyio
async def test_delete_workspace_with_negative_balance_preserves_debt_without_forfeiture() -> None:
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Debt Corp", status=WorkspaceStatus.ACTIVE),
        balance=Decimal("-15.00"),
    )

    result = await use_case.execute(
        workspace_id=ws_id,
        confirmation_name="Debt Corp",
        user_id=42,
    )

    assert result == DeleteWorkspaceResult(workspace_id=ws_id, forfeited_balance=Decimal("0"))
    assert store.forfeited is None
    assert store.deleted == (ws_id, 42)
    assert uow.committed is True
    assert gateway.calls == [(ws_id, f"ws-delete-sub:{ws_id}", True, False)]


@pytest.mark.anyio
async def test_delete_workspace_stripe_gateway_failure_after_commit() -> None:
    """If Stripe gateway fails after commit, workspace remains committed deleted (ADR 0003 §5)."""
    ws_id = uuid4()
    use_case, store, uow, gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner=True,
        balance=Decimal("45.50"),
        gateway_fail=True,
    )

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
    assert [c[0] for c in gateway.calls] == [ws_id]


# ==============================================================================
# HTTP endpoint tests
# ==============================================================================


def test_delete_workspace_http_204_success(
    override_delete_workspace: Callable[[DeleteWorkspace], None],
) -> None:
    ws_id = uuid4()
    use_case, store, _uow, _gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner=True,
        balance=Decimal("25.00"),
    )
    override_delete_workspace(use_case)

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme Corp"},
    )
    assert resp.status_code == 204
    assert store.deleted == (ws_id, 42)
    assert store.forfeited == (ws_id, Decimal("25.00"))


def test_delete_workspace_http_422_name_mismatch(
    override_delete_workspace: Callable[[DeleteWorkspace], None],
) -> None:
    ws_id = uuid4()
    use_case, _store, _uow, _gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner=True,
    )
    override_delete_workspace(use_case)

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Different Name"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "workspace_name_mismatch"


def test_delete_workspace_http_403_non_owner(
    override_delete_workspace: Callable[[DeleteWorkspace], None],
) -> None:
    ws_id = uuid4()
    use_case, _store, _uow, _gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.ACTIVE),
        is_owner=False,
    )
    override_delete_workspace(use_case)

    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme Corp"},
    )
    assert resp.status_code == 403
    assert "owner" in resp.json()["detail"].lower()


def test_delete_workspace_http_404_when_workspace_not_in_token_scope(
    override_delete_workspace: Callable[[DeleteWorkspace], None],
) -> None:
    ws_id = uuid4()
    other_ws_id = uuid4()
    dummy_use_case, _store, _uow, _gateway = _make_use_case()
    override_delete_workspace(dummy_use_case)

    client = authenticated_test_client(app, workspace_ids=[other_ws_id], user_id=42)
    resp = client.request(
        "DELETE",
        f"/api/workspaces/{ws_id}",
        json={"confirmation_name": "Acme Corp"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "workspace not found"


def test_delete_workspace_http_404_when_workspace_already_deleted(
    override_delete_workspace: Callable[[DeleteWorkspace], None],
) -> None:
    ws_id = uuid4()
    use_case, _store, _uow, _gateway = _make_use_case(
        workspace=WorkspaceRecord(id=ws_id, name="Acme Corp", status=WorkspaceStatus.DELETED),
        is_owner=True,
    )
    override_delete_workspace(use_case)

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
    override_delete_workspace: Callable[[DeleteWorkspace], None],
    invalid_payload: dict[str, object],
) -> None:
    ws_id = uuid4()
    dummy_use_case, _store, _uow, _gateway = _make_use_case()
    override_delete_workspace(dummy_use_case)

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


def test_get_delete_workspace_provider_503_without_resources() -> None:
    from fastapi import HTTPException
    from starlette.requests import Request

    dummy_app = type("App", (), {"state": type("State", (), {"reviews_api_resources": None})()})()
    request = Request({"type": "http", "app": dummy_app})

    with pytest.raises(HTTPException) as exc_info:
        get_delete_workspace(request)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Database is not configured"


def test_delete_workspace_http_503_when_database_not_configured_without_override() -> None:
    ws_id = uuid4()
    app.dependency_overrides.pop(get_delete_workspace, None)
    client = authenticated_test_client(app, workspace_ids=[ws_id], user_id=42)
    old_resources = getattr(app.state, "reviews_api_resources", None)
    try:
        app.state.reviews_api_resources = None
        resp = client.request(
            "DELETE",
            f"/api/workspaces/{ws_id}",
            json={"confirmation_name": "Acme"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Database is not configured"
    finally:
        app.state.reviews_api_resources = old_resources


@pytest.mark.anyio
async def test_delete_workspace_real_provider_contract_validates_before_payment_gateway() -> None:
    ws_id = uuid4()
    store = FakeDeleteWorkspaceStore(
        workspace=WorkspaceRecord(id=ws_id, name="Acme", status=WorkspaceStatus.ACTIVE),
        is_owner_val=True,
    )
    uow = FakeDeleteWorkspaceUnitOfWork(store)
    from app.bootstrap.reviews_api import _UnconfiguredPaymentGateway

    use_case = DeleteWorkspace(
        uow_factory=lambda: uow,
        payment_gateway=_UnconfiguredPaymentGateway(),
    )

    # 1. Non-existent workspace raises WorkspaceNotFound (404), NOT NotImplementedError
    missing_id = uuid4()
    with pytest.raises(WorkspaceNotFound):
        await use_case.execute(
            workspace_id=missing_id,
            confirmation_name="Acme",
            user_id=42,
        )

    # 2. Non-owner raises NotWorkspaceOwner (403), NOT NotImplementedError
    store.is_owner_val = False
    with pytest.raises(NotWorkspaceOwner):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme",
            user_id=42,
        )
    store.is_owner_val = True

    # 3. Name mismatch raises WorkspaceNameMismatch (422), NOT NotImplementedError
    with pytest.raises(WorkspaceNameMismatch):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Wrong Name",
            user_id=42,
        )

    # 4. When checks pass it reaches ensure_configured() and raises NotImplementedError (500)
    with pytest.raises(NotImplementedError):
        await use_case.execute(
            workspace_id=ws_id,
            confirmation_name="Acme",
            user_id=42,
        )
