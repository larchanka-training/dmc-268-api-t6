"""Ports of the indexing job besides the store and the VCS (#142).

`IndexRepositoryReader` names the repository a job indexes; `IndexWakeups` puts a new
`index.repo` wake-up on the queue (PR 3 implements it; the #143 triggers reuse it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class IndexedRepository:
    """What a snapshot source of the repository is built from."""

    repository_id: UUID
    provider: str
    installation_external_id: int
    full_name: str


class IndexRepositoryReader(Protocol):
    async def get(self, repository_id: UUID) -> IndexedRepository | None:
        """The repository, or `None` when it no longer exists; one short read."""
        ...


class IndexWakeups(Protocol):
    async def wake(self, repository_id: UUID, generation: int) -> None:
        """Enqueue a new wake-up of the repository's index job; never inside a
        transaction."""
        ...
