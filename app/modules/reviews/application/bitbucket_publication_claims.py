"""Short fenced publication-claim transactions owned by the use case."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class PublicationClaim:
    run_id: UUID
    owner_id: UUID
    generation: int


class PublicationClaimStore(Protocol):
    async def acquire(self, run_id: UUID) -> PublicationClaim: ...

    async def renew(self, claim: PublicationClaim) -> None: ...

    async def release(self, claim: PublicationClaim) -> None: ...


class PublicationClaimUnitOfWork(UnitOfWork, Protocol):
    @property
    def claims(self) -> PublicationClaimStore: ...


class BitbucketPublicationClaims:
    """Commit a claim mutation before the caller may perform provider HTTP."""

    def __init__(self, uow_factory: Callable[[], PublicationClaimUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def acquire(self, run_id: UUID) -> PublicationClaim:
        async with self._uow_factory() as uow:
            claim = await uow.claims.acquire(run_id)
            await uow.commit()
        return claim

    async def renew(self, claim: PublicationClaim) -> None:
        async with self._uow_factory() as uow:
            await uow.claims.renew(claim)
            await uow.commit()

    async def release(self, claim: PublicationClaim) -> None:
        async with self._uow_factory() as uow:
            await uow.claims.release(claim)
            await uow.commit()
