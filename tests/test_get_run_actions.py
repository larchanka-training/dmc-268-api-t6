import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app, get_run_repository
from app.modules.reviews.application.get_run_actions import (
    GetRunActionResponse,
    GetRunActions,
    RunAction,
    RunActionResponse,
)


class FakeActionsRepository:
    def __init__(
        self,
        actions: list[RunAction] | None,
        responses: dict[int, RunActionResponse] | None = None,
    ) -> None:
        self.actions = actions
        self.responses = responses or {}
        self.action_calls: list[UUID] = []
        self.response_calls: list[tuple[UUID, int]] = []

    async def get_run_actions(self, run_id: UUID) -> list[RunAction] | None:
        self.action_calls.append(run_id)
        return self.actions

    async def get_run_action_response(self, run_id: UUID, index: int) -> RunActionResponse | None:
        self.response_calls.append((run_id, index))
        return self.responses.get(index)


def make_action(
    index: int,
    *,
    response: Any | None = None,
    response_ref: str | None = None,
) -> RunAction:
    return RunAction(
        id=UUID(f"00000000-0000-0000-0000-{index + 1:012d}"),
        run_id=UUID("00000000-0000-0000-0000-000000000100"),
        index=index,
        tool="github.get_file",
        request={"path": "app/service.py"},
        response=response,
        response_ref=response_ref,
        started_at=datetime(2026, 9, 24, tzinfo=UTC),
        duration_ms=42,
    )


def test_get_run_actions_returns_small_and_exactly_64_kib_responses_inline() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    boundary_response = {"payload": "x" * 65522}
    repository = FakeActionsRepository(
        [make_action(0, response={"ok": True}), make_action(1, response=boundary_response)]
    )
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/actions")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "runId": str(run_id),
            "index": 0,
            "tool": "github.get_file",
            "request": {"path": "app/service.py"},
            "response": {"ok": True},
            "responseRef": None,
            "startedAt": "2026-09-24T00:00:00Z",
            "durationMs": 42,
        },
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "runId": str(run_id),
            "index": 1,
            "tool": "github.get_file",
            "request": {"path": "app/service.py"},
            "response": boundary_response,
            "responseRef": None,
            "startedAt": "2026-09-24T00:00:00Z",
            "durationMs": 42,
        },
    ]


def test_get_run_actions_redacts_oversized_response_and_preserves_external_reference() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    repository = FakeActionsRepository(
        [
            make_action(2, response={"payload": "x" * 65523}),
            make_action(3, response_ref="s3://trace/run-100/action-3.json"),
        ]
    )
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/actions")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "runId": str(run_id),
            "index": 2,
            "tool": "github.get_file",
            "request": {"path": "app/service.py"},
            "response": None,
            "responseRef": f"/api/runs/{run_id}/actions/2/response",
            "startedAt": "2026-09-24T00:00:00Z",
            "durationMs": 42,
        },
        {
            "id": "00000000-0000-0000-0000-000000000004",
            "runId": str(run_id),
            "index": 3,
            "tool": "github.get_file",
            "request": {"path": "app/service.py"},
            "response": None,
            "responseRef": "s3://trace/run-100/action-3.json",
            "startedAt": "2026-09-24T00:00:00Z",
            "durationMs": 42,
        },
    ]


def test_get_run_action_response_returns_the_complete_stored_response() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    full_response = {"payload": "x" * 65523}
    repository = FakeActionsRepository(
        [make_action(2, response=full_response)], {2: RunActionResponse(full_response)}
    )
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/actions/2/response")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == full_response


def test_get_run_action_response_returns_null_for_an_existing_action() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    repository = FakeActionsRepository([make_action(2)], {2: RunActionResponse(response=None)})
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/actions/2/response")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() is None


def test_run_action_use_cases_delegate_to_the_repository() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    action = make_action(0, response={"ok": True})
    repository = FakeActionsRepository([action], {0: RunActionResponse({"ok": True})})

    actions = asyncio.run(GetRunActions(repository).execute(run_id))
    full_response = asyncio.run(GetRunActionResponse(repository).execute(run_id, 0))

    assert actions is not None
    assert actions[0].response == {"ok": True}
    assert actions[0].response_ref is None
    assert full_response == RunActionResponse({"ok": True})
    assert repository.action_calls == [run_id]
    assert repository.response_calls == [(run_id, 0)]


def test_run_actions_return_404_for_missing_run_or_action_and_422_for_invalid_parameters() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    repository = FakeActionsRepository(None)
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        missing_run = client.get(f"/api/runs/{run_id}/actions")
        missing_action = client.get(f"/api/runs/{run_id}/actions/0/response")
        invalid_run = client.get("/api/runs/not-a-uuid/actions")
        invalid_index = client.get(f"/api/runs/{run_id}/actions/-1/response")
    finally:
        app.dependency_overrides.clear()

    assert missing_run.status_code == 404
    assert missing_action.status_code == 404
    assert invalid_run.status_code == 422
    assert invalid_index.status_code == 422
