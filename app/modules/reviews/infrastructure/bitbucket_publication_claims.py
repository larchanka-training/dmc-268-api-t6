"""Short PostgreSQL publication lease with a monotonically increasing owner fence."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete, func, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.bitbucket.infrastructure.models import BitbucketPublicationClaim
from app.modules.reviews.application.bitbucket_publication_claims import PublicationClaim
from app.modules.reviews.application.publish_run_review import ObsoletePublication

_LEASE = text("interval '90 seconds'")


class SqlAlchemyBitbucketPublicationClaimStore:
    """Fenced claim mutations in the active use-case transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        generation = await self._session.scalar(statement)
        if generation is None:
            raise ObsoletePublication
        return PublicationClaim(run_id, owner, generation)

    async def renew(self, claim: PublicationClaim) -> None:
        table = BitbucketPublicationClaim
        renewed = await self._session.scalar(
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
        await self._session.execute(
            delete(table).where(
                table.run_id == claim.run_id,
                table.owner_id == claim.owner_id,
                table.generation == claim.generation,
            )
        )


class SqlAlchemyBitbucketPublicationClaimUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def claims(self) -> SqlAlchemyBitbucketPublicationClaimStore:
        return SqlAlchemyBitbucketPublicationClaimStore(self.session)
