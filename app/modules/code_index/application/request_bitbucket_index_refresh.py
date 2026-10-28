"""Wake an active Bitbucket repository index for a signed default-branch push."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork


class BitbucketIndexRefreshRepositories(Protocol):
    async def resolve(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> UUID | None: ...


class BitbucketIndexRequestStore(Protocol):
    async def request_build(
        self, repository_id: UUID, *, target_commit_sha: str | None
    ) -> bool: ...


class BitbucketIndexRefreshUnitOfWork(UnitOfWork, Protocol):
    @property
    def repositories(self) -> BitbucketIndexRefreshRepositories: ...

    @property
    def index(self) -> BitbucketIndexRequestStore: ...


class RequestBitbucketIndexRefresh:
    """Own the short refresh transaction; provider I/O has already completed."""

    def __init__(self, uow: Callable[[], BitbucketIndexRefreshUnitOfWork]) -> None:
        self._uow = uow

    async def execute(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> bool:
        if not branches:
            return False
        async with self._uow() as uow:
            eligible = await uow.repositories.resolve(
                installation_id,
                repository_id,
                repository_uuid,
                branches,
                binding_generation,
                webhook_generation,
            )
            if eligible is None:
                return False
            requested = await uow.index.request_build(eligible, target_commit_sha=None)
            await uow.commit()
            return requested
