"""Short status-intent transactions owned by the reconciliation use case."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.bitbucket_status import BitbucketStatusSnapshot


@dataclass(frozen=True)
class StatusKey:
    workspace_id: UUID
    repository_uuid: UUID
    pr_number: int
    head_sha: str


@dataclass(frozen=True)
class StatusIntent:
    key: StatusKey
    run_id: UUID
    desired_state: str
    run_url: str
    applied_run_id: UUID | None
    applied_state: str | None


@dataclass(frozen=True)
class StatusClaim:
    key: StatusKey
    owner_id: UUID
    generation: int


def status_key(snapshot: BitbucketStatusSnapshot) -> StatusKey:
    return StatusKey(
        snapshot.target.workspace_id,
        snapshot.target.repository_uuid,
        snapshot.pr_number,
        snapshot.head_sha,
    )


class StatusIntentStore(Protocol):
    async def register(self, snapshot: BitbucketStatusSnapshot, run_url: str) -> None: ...

    async def acquire(self, key: StatusKey) -> StatusClaim | None: ...

    async def renew(self, claim: StatusClaim) -> bool: ...

    async def intent(self, claim: StatusClaim) -> StatusIntent | None: ...

    async def mark_applied(self, claim: StatusClaim, intent: StatusIntent) -> bool: ...

    async def forbid(self, claim: StatusClaim, credential_generation: int) -> None: ...

    async def defer_rate_limit(self, claim: StatusClaim, delay: timedelta) -> None: ...

    async def release(self, claim: StatusClaim) -> None: ...

    async def retire(self, run_id: UUID) -> None: ...

    async def pending(self, *, limit: int = 100) -> list[tuple[UUID, str]]: ...


class StatusIntentUnitOfWork(UnitOfWork, Protocol):
    @property
    def statuses(self) -> StatusIntentStore: ...


class BitbucketStatusIntents:
    """Commit each fenced mutation before a caller can make a provider request."""

    def __init__(self, uow_factory: Callable[[], StatusIntentUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def register(self, snapshot: BitbucketStatusSnapshot, run_url: str) -> None:
        async with self._uow_factory() as uow:
            await uow.statuses.register(snapshot, run_url)
            await uow.commit()

    async def acquire(self, key: StatusKey) -> StatusClaim | None:
        async with self._uow_factory() as uow:
            claim = await uow.statuses.acquire(key)
            await uow.commit()
        return claim

    async def renew(self, claim: StatusClaim) -> bool:
        async with self._uow_factory() as uow:
            renewed = await uow.statuses.renew(claim)
            await uow.commit()
        return renewed

    async def intent(self, claim: StatusClaim) -> StatusIntent | None:
        async with self._uow_factory() as uow:
            return await uow.statuses.intent(claim)

    async def mark_applied(self, claim: StatusClaim, intent: StatusIntent) -> bool:
        async with self._uow_factory() as uow:
            applied = await uow.statuses.mark_applied(claim, intent)
            await uow.commit()
        return applied

    async def forbid(self, claim: StatusClaim, credential_generation: int) -> None:
        async with self._uow_factory() as uow:
            await uow.statuses.forbid(claim, credential_generation)
            await uow.commit()

    async def defer_rate_limit(self, claim: StatusClaim, delay: timedelta) -> None:
        async with self._uow_factory() as uow:
            await uow.statuses.defer_rate_limit(claim, delay)
            await uow.commit()

    async def release(self, claim: StatusClaim) -> None:
        async with self._uow_factory() as uow:
            await uow.statuses.release(claim)
            await uow.commit()

    async def retire(self, run_id: UUID) -> None:
        async with self._uow_factory() as uow:
            await uow.statuses.retire(run_id)
            await uow.commit()

    async def pending(self, *, limit: int = 100) -> list[tuple[UUID, str]]:
        async with self._uow_factory() as uow:
            return await uow.statuses.pending(limit=limit)
