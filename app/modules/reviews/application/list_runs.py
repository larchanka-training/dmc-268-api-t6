"""Application service and read-model contract for the run list."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class RunCursor:
    """Opaque continuation position for a descending ``(created_at, id)`` listing."""

    created_at: datetime
    id: UUID

    def encode(self) -> str:
        payload = json.dumps(
            {"createdAt": self.created_at.isoformat(), "id": str(self.id)}, separators=(",", ":")
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> RunCursor:
        try:
            padding = "=" * (-len(value) % 4)
            decoded = base64.urlsafe_b64decode(value + padding).decode()
        except (binascii.Error, ValueError) as error:
            raise ValueError("invalid cursor") from error
        try:
            payload = json.loads(decoded)
            created_at = datetime.fromisoformat(payload["createdAt"])
            if created_at.tzinfo is None:
                raise ValueError("cursor timestamp must include a timezone")
            return cls(created_at=created_at, id=UUID(payload["id"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("invalid cursor") from error


@dataclass(frozen=True)
class RunListItem:
    id: UUID
    status: str
    engine: str
    attempt: int
    cancel_requested: bool
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    model: str | None
    action_count: int
    repo: str
    number: int
    title: str
    url: str
    head_sha: str
    created_at: datetime


@dataclass(frozen=True)
class RunListPage:
    items: list[RunListItem]
    next_cursor: str | None


class RunRepository(Protocol):
    async def list_runs(
        self,
        *,
        status: str | None,
        repository: str | None,
        cursor: RunCursor | None,
        limit: int,
    ) -> list[RunListItem]: ...


class ListRuns:
    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        *,
        status: str | None = None,
        repository: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunListPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        decoded_cursor = RunCursor.decode(cursor) if cursor is not None else None
        items = await self._repository.list_runs(
            status=status,
            repository=repository,
            cursor=decoded_cursor,
            limit=limit + 1,
        )
        page_items = items[:limit]
        next_cursor = None
        if len(items) > limit:
            last_item = page_items[-1]
            next_cursor = RunCursor(created_at=last_item.created_at, id=last_item.id).encode()
        return RunListPage(items=page_items, next_cursor=next_cursor)
