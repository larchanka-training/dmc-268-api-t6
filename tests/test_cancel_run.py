from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.main import app, get_cancel_run, get_run_repository
from app.modules.reviews.application.cancel_run import (
    CancelRequestResult,
    CancelRun,
    CancelRunRepository,
)
from app.modules.reviews.application.get_run import RunReview
from app.modules.reviews.application.list_runs import RunListItem
from tests.portal_test_client import authenticated_test_client as TestClient
from tests.test_runs_api import empty_review


class FakeCancelRunRepository:
    def __init__(self, items: list[RunListItem]) -> None:
        self._items = {item.id: item for item in items}

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        item = self._items.get(run_id)
        if item is None:
            return CancelRequestResult(found=False, changed=False)
        if item.status == "queued":
            self._items[run_id] = replace(item, status="cancelled")
            return CancelRequestResult(found=True, changed=True)
        elif item.status in {"running", "publishing"}:
            if not item.cancel_requested:
                self._items[run_id] = replace(item, cancel_requested=True)
                return CancelRequestResult(found=True, changed=True)
        return CancelRequestResult(found=True, changed=False)

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        return self._items.get(run_id)

    async def get_run_review(self, run_id: UUID) -> RunReview | None:
        return empty_review() if run_id in self._items else None


class StubCancelRunRepository:
    def __init__(self, item: RunListItem | None, exists: bool, changed: bool = True) -> None:
        self._item = item
        self._exists = exists
        self._changed = changed
        self.requested_id: UUID | None = None

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        self.requested_id = run_id
        return CancelRequestResult(found=self._exists, changed=self._changed)

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        return self._item


class FakeCancelRunUow:
    def __init__(self, repository: CancelRunRepository) -> None:
        self.repository = repository
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> FakeCancelRunUow:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.rolled_back = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def make_item(value: int, status: str) -> RunListItem:
    created_at = datetime(2026, 9, 25, tzinfo=UTC)
    return RunListItem(
        id=UUID(f"00000000-0000-0000-0000-{value:012d}"),
        status=status,
        engine="fast",
        attempt=0,
        cancel_requested=False,
        started_at=created_at,
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        repo="org/repo",
        number=value,
        title=f"PR {value}",
        url=f"https://example.test/{value}",
        head_sha="a" * 40,
        created_at=created_at,
        trigger="webhook",
    )


def test_cancel_run_returns_the_updated_run() -> None:
    item = make_item(1, "running")
    repository = StubCancelRunRepository(item, exists=True)
    uow = FakeCancelRunUow(repository)

    result = asyncio.run(CancelRun(uow_factory=lambda: uow).execute(item.id))

    assert result == item
    assert repository.requested_id == item.id
    assert uow.committed is True


def test_cancel_run_rolls_back_and_does_not_commit_on_failure() -> None:
    class FailingCancelRunRepository(StubCancelRunRepository):
        async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
            raise RuntimeError("db error")

    item = make_item(1, "running")
    repository = FailingCancelRunRepository(item, exists=True)
    uow = FakeCancelRunUow(repository)

    with pytest.raises(RuntimeError, match="db error"):
        asyncio.run(CancelRun(uow_factory=lambda: uow).execute(item.id))

    assert uow.committed is False
    assert uow.rolled_back is True


def test_cancel_run_returns_none_for_a_missing_run() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000999")
    repository = StubCancelRunRepository(None, exists=False)
    uow = FakeCancelRunUow(repository)

    result = asyncio.run(CancelRun(uow_factory=lambda: uow).execute(run_id))

    assert result is None
    assert repository.requested_id == run_id
    assert uow.committed is False


def test_cancel_queued_run_transitions_to_cancelled() -> None:
    item = make_item(1, "queued")
    repository = FakeCancelRunRepository([item])
    app.dependency_overrides[get_cancel_run] = lambda: CancelRun(
        uow_factory=lambda: FakeCancelRunUow(repository)
    )
    try:
        response = TestClient(app).post(f"/api/runs/{item.id}/cancel")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancelRequested"] is False


@pytest.mark.parametrize("status", ["running", "publishing"])
def test_cancel_active_run_is_idempotent_and_does_not_change_another_run(status: str) -> None:
    item = make_item(1, status)
    other = make_item(2, "running")
    repository = FakeCancelRunRepository([item, other])
    app.dependency_overrides[get_cancel_run] = lambda: CancelRun(
        uow_factory=lambda: FakeCancelRunUow(repository)
    )
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        first = client.post(f"/api/runs/{item.id}/cancel")
        second = client.post(f"/api/runs/{item.id}/cancel")
        other_response = client.get(f"/api/runs/{other.id}")
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == status
    assert first.json()["cancelRequested"] is True
    assert second.json() == first.json()
    assert other_response.status_code == 200
    assert other_response.json()["status"] == "running"
    assert other_response.json()["cancelRequested"] is False


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "skipped"])
def test_cancel_terminal_run_is_an_idempotent_noop(status: str) -> None:
    item = make_item(1, status)
    repository = FakeCancelRunRepository([item])
    app.dependency_overrides[get_cancel_run] = lambda: CancelRun(
        uow_factory=lambda: FakeCancelRunUow(repository)
    )
    try:
        response = TestClient(app).post(f"/api/runs/{item.id}/cancel")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["cancelRequested"] is False


def test_cancel_returns_404_for_missing_run_and_422_for_invalid_id() -> None:
    repository = FakeCancelRunRepository([])
    app.dependency_overrides[get_cancel_run] = lambda: CancelRun(
        uow_factory=lambda: FakeCancelRunUow(repository)
    )
    try:
        client = TestClient(app)
        missing = client.post("/api/runs/00000000-0000-0000-0000-000000000999/cancel")
        invalid = client.post("/api/runs/not-a-uuid/cancel")
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    assert invalid.status_code == 422
