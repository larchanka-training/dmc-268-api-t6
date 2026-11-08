"""The SQL of the run data retention task (#457, SD §10)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.reviews.application.handle_review_run import TERMINAL_STATES
from app.modules.reviews.infrastructure.models import (
    CachedFileBlob,
    CodeChangeDiff,
    ContextManifest,
    ContextPayload,
    Run,
)


class SqlAlchemyRunDataRetentionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def purge_expired_runs(self, before: datetime, now: datetime, limit: int) -> int:
        """Lock a batch, clear it and mark it; a run another pass holds is skipped.

        The diff rows stay without their text: `summaryOnly` and the fallback verdict read
        them. A context payload with the status `summary_only` stays: it is the other source
        of `summaryOnly` and the source of its reason in the check-run, and it holds counts and
        paths, no code. `updated_at` stays too: it is the id of the run's SSE event, and a
        cleanup is no event for the clients following the run.
        """
        run_ids = list(
            await self._session.scalars(
                select(Run.id)
                .where(
                    Run.state.in_(TERMINAL_STATES),
                    Run.finished_at < before,
                    Run.data_purged_at.is_(None),
                )
                .order_by(Run.finished_at, Run.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        if not run_ids:
            return 0
        await self._session.execute(
            update(CodeChangeDiff)
            .where(CodeChangeDiff.run_id.in_(run_ids))
            .values(patch=None, review_patch=None)
        )
        await self._session.execute(
            delete(ContextManifest).where(ContextManifest.run_id.in_(run_ids))
        )
        await self._session.execute(
            delete(ContextPayload).where(
                ContextPayload.run_id.in_(run_ids),
                ContextPayload.summary["status"].astext.is_distinct_from("summary_only"),
            )
        )
        await self._session.execute(
            update(Run)
            .where(Run.id.in_(run_ids))
            .values(data_purged_at=now, updated_at=Run.updated_at)
        )
        return len(run_ids)

    async def clear_expired_blobs(self, now: datetime, limit: int) -> int:
        """Drop the text of expired blobs; a later `put` of the same blob brings it back.

        A blob belongs to its repository, not to a run: many runs share one, so its own
        expiry decides. Past it every reader already gets `EXPIRED` and no content. The row
        stays because `/files` answers 410 only with it; without it the answer would be 404.
        """
        blob_ids = list(
            await self._session.scalars(
                select(CachedFileBlob.id)
                .where(CachedFileBlob.expires_at <= now, CachedFileBlob.content != "")
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        if blob_ids:
            await self._session.execute(
                update(CachedFileBlob).where(CachedFileBlob.id.in_(blob_ids)).values(content="")
            )
        return len(blob_ids)


class SqlAlchemyRunDataRetentionUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def retention(self) -> SqlAlchemyRunDataRetentionStore:
        return SqlAlchemyRunDataRetentionStore(self.session)
