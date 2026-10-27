"""Short PostgreSQL publication lease with a monotonically increasing owner fence."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import delete, func, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.bitbucket.infrastructure.models import BitbucketPublicationClaim
from app.modules.reviews.application.publish_run_review import ObsoletePublication

_LEASE = text("interval '90 seconds'")


@dataclass(frozen=True)
class PublicationClaim:
    run_id: UUID
    owner_id: UUID
    generation: int


class BitbucketPublicationClaims:
    """Each method owns a short transaction that ends before any provider HTTP."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def acquire(self, run_id: UUID) -> PublicationClaim:
        owner = uuid4()
        table = BitbucketPublicationClaim
        statement = (
            insert(table)
            .values(
                run_id=run_id,
                owner_id=owner,
                generation=1,
                lease_until=func.now() + _LEASE,
            )
            .on_conflict_do_update(
                index_elements=[table.run_id],
                set_={
                    "owner_id": owner,
                    "generation": table.generation + 1,
                    "lease_until": func.now() + _LEASE,
                },
                where=table.lease_until <= func.now(),
            )
            .returning(table.generation)
        )
        async with self._sessions.begin() as session:
            generation = await session.scalar(statement)
        if generation is None:
            raise ObsoletePublication
        return PublicationClaim(run_id, owner, generation)

    async def renew(self, claim: PublicationClaim) -> None:
        table = BitbucketPublicationClaim
        async with self._sessions.begin() as session:
            renewed = await session.scalar(
                update(table)
                .where(
                    table.run_id == claim.run_id,
                    table.owner_id == claim.owner_id,
                    table.generation == claim.generation,
                    table.lease_until > func.now(),
                )
                .values(lease_until=func.now() + _LEASE)
                .returning(table.run_id)
            )
        if renewed is None:
            raise ObsoletePublication

    async def release(self, claim: PublicationClaim) -> None:
        table = BitbucketPublicationClaim
        async with self._sessions.begin() as session:
            await session.execute(
                delete(table).where(
                    table.run_id == claim.run_id,
                    table.owner_id == claim.owner_id,
                    table.generation == claim.generation,
                )
            )
