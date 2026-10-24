"""PostgreSQL adapter of the reviews module's `ReviewIndexAccess` port (#144, ADR 0001 §4, §6.1).

One READ COMMITTED transaction of the caller, with no network call. The effective RAG is
read first without a lock: off returns at once, with no lock and no state read. On, the
repository row is taken under a share lock and the condition is read again in a statement
of its own, so a concurrent toggle-off is either seen or waits for the review. A writer of
the repository row is waited for only until the lock timeout: the timeout fails the gate,
and the review goes on without RAG instead of waiting until its deadline. The state
row is taken `FOR UPDATE SKIP LOCKED`: a review never waits for a build and only reads a
row another session holds. The marker and the generation are written only through the
store's `request_build` and `enable`; `last_used_at` and the throttle columns by this
adapter's own update, with the time read after the lock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.code_index.application.vcs import HEALABLE_ERROR_CODES
from app.modules.code_index.domain.rag_index import RagIndex, rag_index
from app.modules.code_index.domain.review_trigger import ReviewTriggerAction, review_trigger
from app.modules.code_index.infrastructure.effective_rag import GITHUB_PROVIDER, RAG_ON
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
from app.modules.code_index.infrastructure.models import CodeIndexState
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.rag_index import RagReadiness

# How long a review waits for a writer of the repository row; set for the gate transaction.
_REPOSITORY_LOCK_TIMEOUT = "2s"

# The index of an enabled repository with no state row yet; the converge pass creates it.
_NO_ROW = RagReadiness(
    enabled=True,
    index=rag_index(
        rag_enabled=True,
        status=None,
        indexed_commit_sha=None,
        target_commit_sha=None,
        published_at=None,
        file_count=None,
        symbol_count=None,
        error_code=None,
        last_error_code=None,
    ),
)


def _rag_index(state: CodeIndexState) -> RagIndex:
    return rag_index(
        rag_enabled=True,
        status=state.status,
        indexed_commit_sha=state.indexed_commit_sha,
        target_commit_sha=state.target_commit_sha,
        published_at=state.published_at,
        file_count=state.file_count,
        symbol_count=state.symbol_count,
        error_code=state.error_code,
        last_error_code=state.last_error_code,
    )


def _readiness(state: CodeIndexState | None) -> RagReadiness:
    if state is None:
        return _NO_ROW
    return RagReadiness(
        enabled=True, index=_rag_index(state), default_head_sha=state.target_commit_sha
    )


class SqlAlchemyReviewIndexAccess:
    """`ReviewIndexAccess` on the caller's session; flushes, never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._store = SqlAlchemyCodeIndexStore(session)

    async def _share_repository(self, repository_id: UUID) -> None:
        await self._session.execute(
            select(Repository.id).where(Repository.id == repository_id).with_for_update(read=True)
        )

    async def _rag_on(self, repository_id: UUID) -> bool:
        rag_on = await self._session.scalar(
            select(Repository.id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .where(Repository.id == repository_id, RAG_ON, GITHUB_PROVIDER)
        )
        return rag_on is not None

    async def _read_state(self, repository_id: UUID) -> CodeIndexState | None:
        statement = select(CodeIndexState).where(CodeIndexState.repository_id == repository_id)
        return cast(
            CodeIndexState | None,
            await self._session.scalar(statement.execution_options(populate_existing=True)),
        )

    async def _try_lock_state(self, repository_id: UUID) -> CodeIndexState | None:
        """The state row under this transaction's lock; None when it is missing or another
        session holds it."""
        statement = (
            select(CodeIndexState)
            .where(CodeIndexState.repository_id == repository_id)
            .with_for_update(skip_locked=True)
        )
        return cast(
            CodeIndexState | None,
            await self._session.scalar(statement.execution_options(populate_existing=True)),
        )

    async def open_for_review(self, repository_id: UUID) -> RagReadiness:
        # Off is read without a lock: a review with RAG off waits for no writer of the
        # repository row and reads no state.
        if not await self._rag_on(repository_id):
            return RagReadiness(enabled=False)
        # On: the lock first, the condition again in a statement of its own. Under READ
        # COMMITTED it reads what a writer the lock waited for committed, so a toggle-off
        # in flight is seen once it commits. A writer that outlasts the timeout fails the
        # gate with a lock error, and the stage records it as a failed check.
        await self._session.execute(text(f"SET LOCAL lock_timeout = '{_REPOSITORY_LOCK_TIMEOUT}'"))
        await self._share_repository(repository_id)
        if not await self._rag_on(repository_id):
            return RagReadiness(enabled=False)
        state = await self._try_lock_state(repository_id)
        if state is None:
            # No row, or a row another session holds: read what it committed and write
            # nothing, so the review never waits; the next review does the extension.
            return _readiness(await self._read_state(repository_id))
        # Read after the lock, as the store's own clock.
        now = cast(datetime, await self._session.scalar(select(func.clock_timestamp())))
        target_commit_sha = state.target_commit_sha
        index = _rag_index(state)
        trigger = review_trigger(
            status=index.status,
            usable=index.usable,
            purge_reason=state.purge_reason,
            wake_requested_at=state.wake_requested_at,
            lease_active=state.lease_expires_at is not None and state.lease_expires_at > now,
            last_error_code=state.last_error_code,
            target_commit_sha=target_commit_sha,
            last_used_at=state.last_used_at,
            review_full_requested_at=state.review_full_requested_at,
            review_full_requested_sha=state.review_full_requested_sha,
            healable_error_codes=HEALABLE_ERROR_CODES,
            now=now,
        )
        if trigger.action is ReviewTriggerAction.ENABLE:
            # The decision read `purge_reason == ttl` on the row this transaction holds, so
            # no toggle-off or removal purged it since.
            await self._store.enable(repository_id)
        elif trigger.action is not ReviewTriggerAction.NONE and not (
            # The review never writes the target: the build follows the one the row has.
            await self._store.request_build(repository_id, target_commit_sha=None)
        ):
            # The held row lock makes this unreachable today; kept for a refusal the store may add.
            # A refused request queues nothing, so it extends and throttles nothing.
            return _readiness(await self._read_state(repository_id))
        values: dict[str, Any] = {}
        if trigger.extend_last_used:
            values["last_used_at"] = now
        if trigger.record_throttle:
            values["review_full_requested_at"] = now
            values["review_full_requested_sha"] = target_commit_sha
        if values:
            await self._session.execute(
                update(CodeIndexState)
                .where(CodeIndexState.repository_id == repository_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        return _readiness(await self._read_state(repository_id))


class SqlAlchemyReviewIndexUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def review_index(self) -> SqlAlchemyReviewIndexAccess:
        return SqlAlchemyReviewIndexAccess(self.session)
