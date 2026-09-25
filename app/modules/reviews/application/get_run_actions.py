"""Application services and read-model contracts for run action traces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

INLINE_RESPONSE_LIMIT_BYTES = 64 * 1024


@dataclass(frozen=True)
class RunAction:
    id: UUID
    run_id: UUID
    index: int
    tool: str
    request: dict[str, Any]
    response: Any | None
    response_ref: str | None
    started_at: datetime
    duration_ms: int


@dataclass(frozen=True)
class RunActionTrace:
    id: UUID
    run_id: UUID
    index: int
    tool: str
    request: dict[str, Any]
    response: Any | None
    response_ref: str | None
    started_at: datetime
    duration_ms: int


@dataclass(frozen=True)
class RunActionResponse:
    response: Any | None


class RunActionsRepository(Protocol):
    async def get_run_actions(self, run_id: UUID) -> list[RunAction] | None: ...

    async def get_run_action_response(
        self, run_id: UUID, index: int
    ) -> RunActionResponse | None: ...


class GetRunActions:
    def __init__(self, repository: RunActionsRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> list[RunActionTrace] | None:
        actions = await self._repository.get_run_actions(run_id)
        if actions is None:
            return None
        return [self._to_trace(run_id, action) for action in actions]

    @staticmethod
    def _to_trace(run_id: UUID, action: RunAction) -> RunActionTrace:
        if (
            action.response is not None
            and _json_size(action.response) <= INLINE_RESPONSE_LIMIT_BYTES
        ):
            return RunActionTrace(
                id=action.id,
                run_id=action.run_id,
                index=action.index,
                tool=action.tool,
                request=action.request,
                response=action.response,
                response_ref=None,
                started_at=action.started_at,
                duration_ms=action.duration_ms,
            )
        return RunActionTrace(
            id=action.id,
            run_id=action.run_id,
            index=action.index,
            tool=action.tool,
            request=action.request,
            response=None,
            response_ref=action.response_ref
            or f"/api/runs/{action.run_id}/actions/{action.index}/response",
            started_at=action.started_at,
            duration_ms=action.duration_ms,
        )


class GetRunActionResponse:
    def __init__(self, repository: RunActionsRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID, index: int) -> RunActionResponse | None:
        return await self._repository.get_run_action_response(run_id, index)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
