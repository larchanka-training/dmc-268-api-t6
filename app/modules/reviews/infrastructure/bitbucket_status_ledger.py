"""Durable per-PR/head Bitbucket status intent and short fenced write lease."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    delete,
    false,
    func,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.common.infrastructure.db.enums import RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.bitbucket.infrastructure.models import BitbucketStatusPublication
from app.modules.reviews.application.bitbucket_status import BitbucketStatusSnapshot
from app.modules.reviews.application.bitbucket_status_intents import (
    StatusClaim,
    StatusIntent,
    StatusKey,
)
from app.modules.reviews.infrastructure.models import Run

_LEASE = text("interval '90 seconds'")
_RECHECK = text("interval '30 seconds'")
_TERMINAL_RECHECK = text("interval '5 minutes'")
_TERMINAL_WINDOW = text("interval '1 hour'")


def _rank(state: str) -> int:
    if state == "queued":
        return 0
    if state == "running":
        return 1
    if state == "publishing":
        return 2
    if state in {"succeeded", "failed", "cancelled", "skipped"}:
        return 3
    raise ValueError("invalid Bitbucket status state")


class SqlAlchemyBitbucketStatusLedger:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, snapshot: BitbucketStatusSnapshot, run_url: str) -> None:
        run_order = snapshot.run_order
        if run_order is None or run_order <= 0:
            raise ValueError("persisted Bitbucket status needs a monotonic Run order")
        table = BitbucketStatusPublication
        rank = _rank(snapshot.state)
        fresh_run = table.run_id != snapshot.run_id
        fresh_credential = and_(
            table.blocked_generation.is_not(None),
            table.blocked_generation != snapshot.target.credential_generation,
        )
        requeued = (
            and_(
                table.run_id == snapshot.run_id,
                table.desired_state.in_(("running", "publishing")),
                select(Run.id)
                .where(Run.id == snapshot.run_id, Run.state == RunState.QUEUED)
                .exists(),
            )
            if snapshot.state == "queued"
            else false()
        )
        preserve_backoff = or_(
            table.last_error == "rate_limited",
            and_(
                table.run_id == snapshot.run_id,
                table.desired_state == snapshot.state,
                ~fresh_credential,
            ),
        )
        statement = (
            insert(table)
            .values(
                workspace_id=snapshot.target.workspace_id,
                repository_uuid=snapshot.target.repository_uuid,
                pr_number=snapshot.pr_number,
                head_sha=snapshot.head_sha,
                run_id=snapshot.run_id,
                run_order=run_order,
                run_url=run_url,
                desired_state=snapshot.state,
                desired_rank=rank,
                next_check_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=[
                    table.workspace_id,
                    table.repository_uuid,
                    table.pr_number,
                    table.head_sha,
                ],
                set_={
                    "run_id": snapshot.run_id,
                    "run_order": run_order,
                    "run_url": run_url,
                    "desired_state": snapshot.state,
                    "desired_rank": rank,
                    "next_check_at": case(
                        (preserve_backoff, table.next_check_at), else_=func.now()
                    ),
                    "blocked_generation": case(
                        (or_(fresh_run, fresh_credential), None),
                        else_=table.blocked_generation,
                    ),
                    "last_error": case((preserve_backoff, table.last_error), else_=None),
                },
                where=or_(
                    table.run_order < run_order,
                    and_(table.run_id == snapshot.run_id, table.desired_rank <= rank),
                    requeued,
                ),
            )
        )
        await self._session.execute(statement)

    @staticmethod
    def _key_filter(
        key: StatusKey,
    ) -> tuple[ColumnElement[bool], ColumnElement[bool], ColumnElement[bool], ColumnElement[bool]]:
        table = BitbucketStatusPublication
        return (
            table.workspace_id == key.workspace_id,
            table.repository_uuid == key.repository_uuid,
            table.pr_number == key.pr_number,
            table.head_sha == key.head_sha,
        )

    async def acquire(self, key: StatusKey) -> StatusClaim | None:
        table = BitbucketStatusPublication
        owner = uuid4()
        generation = await self._session.scalar(
            update(table)
            .where(
                *self._key_filter(key),
                or_(table.owner_id.is_(None), table.lease_until <= func.now()),
                table.blocked_generation.is_(None),
                table.next_check_at <= func.now(),
            )
            .values(
                owner_id=owner,
                generation=table.generation + 1,
                lease_until=func.now() + _LEASE,
            )
            .returning(table.generation)
        )
        return None if generation is None else StatusClaim(key, owner, generation)

    async def renew(self, claim: StatusClaim) -> bool:
        table = BitbucketStatusPublication
        renewed = await self._session.scalar(
            update(table)
            .where(
                *self._key_filter(claim.key),
                table.owner_id == claim.owner_id,
                table.generation == claim.generation,
                table.lease_until > func.now(),
            )
            .values(lease_until=func.now() + _LEASE)
            .returning(table.run_id)
        )
        return renewed is not None

    async def intent(self, claim: StatusClaim) -> StatusIntent | None:
        table = BitbucketStatusPublication
        row = (
            await self._session.execute(
                select(
                    table.run_id,
                    table.desired_state,
                    table.run_url,
                    table.applied_run_id,
                    table.applied_state,
                ).where(
                    *self._key_filter(claim.key),
                    table.owner_id == claim.owner_id,
                    table.generation == claim.generation,
                    table.lease_until > func.now(),
                )
            )
        ).one_or_none()
        return (
            None if row is None else StatusIntent(claim.key, row[0], row[1], row[2], row[3], row[4])
        )

    async def mark_applied(self, claim: StatusClaim, intent: StatusIntent) -> bool:
        table = BitbucketStatusPublication
        applied = await self._session.scalar(
            update(table)
            .where(
                *self._key_filter(claim.key),
                table.owner_id == claim.owner_id,
                table.generation == claim.generation,
                table.lease_until > func.now(),
            )
            .values(
                applied_run_id=intent.run_id,
                applied_state=intent.desired_state,
                next_check_at=func.now()
                + case((table.desired_rank == 3, _TERMINAL_RECHECK), else_=_RECHECK),
                last_error=None,
            )
            .returning(table.run_id)
        )
        return applied is not None

    async def forbid(self, claim: StatusClaim, credential_generation: int) -> None:
        table = BitbucketStatusPublication
        await self._session.execute(
            update(table)
            .where(
                *self._key_filter(claim.key),
                table.owner_id == claim.owner_id,
                table.generation == claim.generation,
            )
            .values(
                blocked_generation=credential_generation,
                last_error="author_forbidden",
            )
        )

    async def defer_rate_limit(self, claim: StatusClaim, delay: timedelta) -> None:
        table = BitbucketStatusPublication
        await self._session.execute(
            update(table)
            .where(
                *self._key_filter(claim.key),
                table.owner_id == claim.owner_id,
                table.generation == claim.generation,
            )
            .values(next_check_at=func.now() + delay, last_error="rate_limited")
        )

    async def defer_failure(self, claim: StatusClaim, reason: str, delay: timedelta) -> None:
        if reason not in {"status_unavailable", "pr_missing", "pr_not_open"}:
            raise ValueError("invalid Bitbucket status failure reason")
        table = BitbucketStatusPublication
        await self._session.execute(
            update(table)
            .where(
                *self._key_filter(claim.key),
                table.owner_id == claim.owner_id,
                table.generation == claim.generation,
            )
            .values(next_check_at=func.now() + delay, last_error=reason)
        )

    async def release(self, claim: StatusClaim) -> None:
        table = BitbucketStatusPublication
        await self._session.execute(
            update(table)
            .where(
                *self._key_filter(claim.key),
                table.owner_id == claim.owner_id,
                table.generation == claim.generation,
            )
            .values(owner_id=None, lease_until=None)
        )

    async def retire(self, run_id: UUID) -> None:
        """Remove obsolete intents after the active Run source no longer resolves."""
        await self._session.execute(
            delete(BitbucketStatusPublication).where(BitbucketStatusPublication.run_id == run_id)
        )

    async def retire_expired(self, *, limit: int = 1000) -> int:
        """Delete a bounded batch of converged terminal rows past the replay window."""
        table = BitbucketStatusPublication
        expired = (
            select(table.workspace_id, table.repository_uuid, table.pr_number, table.head_sha)
            .join(Run, Run.id == table.run_id)
            .where(
                table.desired_rank == 3,
                table.applied_run_id == table.run_id,
                table.applied_state == table.desired_state,
                table.blocked_generation.is_(None),
                table.last_error.is_(None),
                cast(Run.state, String(16)) == table.desired_state,
                func.coalesce(Run.finished_at, Run.created_at) + _TERMINAL_WINDOW <= func.now(),
                or_(table.owner_id.is_(None), table.lease_until <= func.now()),
            )
            .limit(limit)
        )
        result = await self._session.execute(
            delete(table)
            .where(
                tuple_(
                    table.workspace_id,
                    table.repository_uuid,
                    table.pr_number,
                    table.head_sha,
                ).in_(expired)
            )
            .returning(table.run_id)
        )
        return len(result.all())

    async def pending(self, *, limit: int = 100) -> list[tuple[UUID, str]]:
        table = BitbucketStatusPublication
        rows = (
            await self._session.execute(
                select(table.run_id, table.run_url)
                .join(Run, Run.id == table.run_id)
                .where(
                    table.blocked_generation.is_(None),
                    table.next_check_at <= func.now(),
                    or_(table.owner_id.is_(None), table.lease_until <= func.now()),
                    or_(
                        table.applied_run_id.is_(None),
                        table.applied_run_id != table.run_id,
                        table.applied_state.is_(None),
                        table.applied_state != table.desired_state,
                        cast(Run.state, String(16)) != table.desired_state,
                        and_(
                            table.desired_rank == 3,
                            func.coalesce(Run.finished_at, Run.created_at) + _TERMINAL_WINDOW
                            > func.now(),
                        ),
                    ),
                )
                .order_by(
                    table.next_check_at,
                    table.workspace_id,
                    table.repository_uuid,
                    table.pr_number,
                )
                .limit(limit)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]


class SqlAlchemyBitbucketStatusUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def statuses(self) -> SqlAlchemyBitbucketStatusLedger:
        return SqlAlchemyBitbucketStatusLedger(self.session)
