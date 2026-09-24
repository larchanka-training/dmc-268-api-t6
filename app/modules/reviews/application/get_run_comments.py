"""Application service and read-model contract for published run comments."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class PublishedComment:
    id: UUID
    path: str
    old_line: int | None
    new_line: int | None
    severity: str
    category: str
    confidence: Decimal
    title: str
    body: str
    suggestion: str | None
    rule_name: str | None


class RunCommentsRepository(Protocol):
    async def get_published_comments(self, run_id: UUID) -> list[PublishedComment] | None: ...


class GetRunComments:
    def __init__(self, repository: RunCommentsRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> list[PublishedComment] | None:
        return await self._repository.get_published_comments(run_id)
