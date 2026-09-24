"""List review runs through an application-facing repository port."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunListItem:
    """A read-model placeholder until the SQLAlchemy projection is wired."""


class RunRepository(Protocol):
    async def list_runs(
        self, *, status: str | None, repository: str | None, limit: int
    ) -> list[RunListItem]: ...


class ListRuns:
    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository

    async def execute(
        self, *, status: str | None = None, repository: str | None = None
    ) -> list[RunListItem]:
        return await self._repository.list_runs(status=status, repository=repository, limit=50)
