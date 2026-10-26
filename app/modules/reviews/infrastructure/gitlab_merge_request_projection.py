"""Short, origin-pinned projection writes and a separately owned advisory channel."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from hashlib import sha256
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.common.infrastructure.db.enums import CodeChangeState, RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabRequestUnavailable,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.infrastructure.models import (
    GitLabConnection,
    GitLabProjectBinding,
)
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.project_github_pull_request import RunCancellationNotice
from app.modules.reviews.application.project_gitlab_merge_request import (
    CanonicalGitLabMergeRequest,
    GitLabMergeRequestCommand,
    GitLabMergeRequestIdentityConflict,
    GitLabMergeRequestRecord,
    LockedGitLabMergeRequest,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.workspaces.infrastructure.models import Workspace

_LOGGER = logging.getLogger(__name__)


def _log_database_error(error: Exception) -> None:
    if not isinstance(error, GitLabRequestUnavailable):
        _LOGGER.error(
            "GitLab projection database operation failed",
            extra={"error_type": type(error).__name__},
        )


def _timeout_milliseconds(seconds: float) -> str:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or not 0 < seconds <= 60
    ):
        raise ValueError("Invalid GitLab projection lock timeout")
    return f"{max(1, math.ceil(seconds * 1000))}ms"


async def _database[T](operation: Awaitable[T]) -> T:
    """Translate only an awaited driver/session operation, never application code."""
    try:
        return await operation
    except GitLabRequestUnavailable:
        raise
    except Exception as error:
        _log_database_error(error)
        raise GitLabRequestUnavailable() from None


class SqlAlchemyGitLabMergeRequestProjectionLock:
    """Own an independent NullPool engine; the bounded data pool is never borrowed.

    One AUTOCOMMIT backend exists per active/waiting hold. Call aclose after all
    holds finish. URL and optional driver connect_args must identify the same DB as
    the data sessions (including any search_path); no engine/connection is shared.
    """

    def __init__(
        self,
        database_url: str | URL,
        *,
        connect_args: dict[str, Any] | None = None,
        lock_timeout_seconds: float = 10,
    ) -> None:
        self._timeout = _timeout_milliseconds(lock_timeout_seconds)
        self._engine = create_async_engine(
            database_url,
            connect_args=connect_args or {},
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        self._closed = False

    async def aclose(self) -> None:
        await _database(self._engine.dispose())
        self._closed = True

    @asynccontextmanager
    async def hold(self, command: GitLabMergeRequestCommand) -> AsyncIterator[None]:
        if self._closed:
            raise GitLabRequestUnavailable()
        identity = (
            f"gitlab:{command.origin_provider_installation_id}:{command.project_id}:{command.iid}"
        )
        key = int.from_bytes(sha256(identity.encode()).digest()[:8], "big", signed=True)
        connection: AsyncConnection | None = None
        acquired = False
        try:
            try:
                connection = await _database(self._engine.connect())
                await _database(
                    connection.execute(
                        select(func.set_config("lock_timeout", self._timeout, False))
                    )
                )
                await _database(
                    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
                )
                acquired = True
            except Exception as error:
                _log_database_error(error)
                raise GitLabRequestUnavailable() from None
            # This yield is intentionally outside the DB error translation boundary.
            yield
        finally:
            failure = sys.exception()
            if connection is not None:
                cleanup = asyncio.create_task(self._release(connection, key, acquired))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Preserve cancellation after channel invalidation/closure.
                    with suppress(GitLabRequestUnavailable):
                        await cleanup
                    raise
                except GitLabRequestUnavailable:
                    if failure is None:
                        raise
                    # Cleanup already invalidated/closed the channel. Keep the error
                    # from the yielded projector, including BaseException cancellation.

    @staticmethod
    async def _release(connection: AsyncConnection, key: int, acquired: bool) -> None:
        failed = False
        try:
            # Each driver cleanup phase is finite even after dispatch cancellation.
            async with asyncio.timeout(5):
                # AUTOCOMMIT has no server transaction; clear SQLAlchemy logical state.
                await connection.rollback()
                if acquired:
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await connection.execute(text("RESET lock_timeout"))
        except Exception as error:
            _log_database_error(error)
            failed = True
            try:
                async with asyncio.timeout(5):
                    await connection.invalidate()
            except Exception as error:
                _log_database_error(error)
                # Closing the independent NullPool connection is still mandatory.
                failed = True
        finally:
            try:
                async with asyncio.timeout(5):
                    await connection.close()
            except Exception as error:
                _log_database_error(error)
                failed = True
                try:
                    async with asyncio.timeout(5):
                        await connection.invalidate()
                except Exception as error:
                    _log_database_error(error)
                    failed = True
        if failed:
            raise GitLabRequestUnavailable() from None


class SqlAlchemyGitLabMergeRequestProjectionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_locked(
        self, current: CanonicalGitLabMergeRequest, now: datetime
    ) -> LockedGitLabMergeRequest:
        values = self._values(current)
        inserted = await _database(
            self._session.scalar(
                insert(CodeChange)
                .values(id=uuid4(), head_first_seen_at=now, **values)
                .on_conflict_do_nothing()
                .returning(CodeChange.id)
            )
        )
        rows = tuple(
            await _database(
                self._session.scalars(
                    select(CodeChange)
                    .where(
                        CodeChange.repository_id == current.target.repository_id,
                        or_(
                            CodeChange.external_id == current.external_id,
                            CodeChange.external_number == current.iid,
                        ),
                    )
                    .order_by(CodeChange.id)
                    .with_for_update()
                )
            )
        )
        if (
            len(rows) != 1
            or rows[0].external_id != current.external_id
            or rows[0].external_number != current.iid
        ):
            raise GitLabMergeRequestIdentityConflict()
        row = rows[0]
        return LockedGitLabMergeRequest(
            GitLabMergeRequestRecord(
                row.id, row.head_sha, row.provider_updated_at, row.head_first_seen_at
            ),
            inserted is not None,
        )

    @staticmethod
    def _values(current: CanonicalGitLabMergeRequest) -> dict[str, Any]:
        return dict(
            repository_id=current.target.repository_id,
            external_id=current.external_id,
            external_number=current.iid,
            title=current.title,
            description=current.description,
            author_login=current.author_login,
            source_branch=current.source_branch,
            target_branch=current.target_branch,
            base_sha=current.diff_refs.start_sha,
            head_sha=current.diff_refs.head_sha,
            state=CodeChangeState("open" if current.state == "opened" else current.state.value),
            provider_updated_at=current.provider_updated_at,
            web_url=current.web_url,
        )

    async def save(
        self, record: GitLabMergeRequestRecord, current: CanonicalGitLabMergeRequest
    ) -> None:
        await _database(
            self._session.execute(
                update(CodeChange)
                .where(CodeChange.id == record.id)
                .values(**self._values(current), head_first_seen_at=record.head_first_seen_at)
            )
        )
        await _database(self._session.flush())


class SqlAlchemyGitLabMergeRequestRunCanceller:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def cancel_for_merge_request(
        self,
        target: BoundGitLabTarget,
        code_change_id: UUID,
        reason: str,
        now: datetime,
        *,
        current_head: str,
    ) -> tuple[RunCancellationNotice, ...]:
        conditions = [
            Run.code_change_id == code_change_id,
            Run.origin_provider_installation_id == target.origin_provider_installation_id,
            Run.state.in_((RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING)),
        ]
        if reason == "superseded":
            conditions.append(Run.head_sha != current_head)
        runs = tuple(
            await _database(
                self._session.scalars(
                    select(Run).where(*conditions).order_by(Run.id).with_for_update()
                )
            )
        )
        notices: list[RunCancellationNotice] = []
        for run in runs:
            if run.state == RunState.QUEUED:
                run.state = RunState.CANCELLED
                run.error_code = reason
                run.finished_at = now
                if run.attempt >= 1 and run.cancellation_signal_requested_at is None:
                    run.cancellation_signal_requested_at = now
                notices.append(RunCancellationNotice(run.id, target.workspace_id, "cancelled"))
            elif not run.cancel_requested:
                run.cancel_requested = True
        await _database(self._session.flush())
        return tuple(notices)

    async def notify_run_updated(self, notice: RunCancellationNotice) -> None:
        await _database(
            self._session.execute(
                select(
                    func.pg_notify(
                        "run_updated",
                        json.dumps(
                            {
                                "run_id": str(notice.run_id),
                                "workspace_id": str(notice.workspace_id),
                                "status": notice.status,
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
            )
        )


class SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, lock_timeout_seconds: float = 10
    ) -> None:
        super().__init__(session_factory)
        self._reader = SqlAlchemyGitLabTargetReader(session_factory)
        self._timeout = _timeout_milliseconds(lock_timeout_seconds)

    @property
    def merge_requests(self) -> SqlAlchemyGitLabMergeRequestProjectionStore:
        return SqlAlchemyGitLabMergeRequestProjectionStore(self.session)

    @property
    def runs(self) -> SqlAlchemyGitLabMergeRequestRunCanceller:
        return SqlAlchemyGitLabMergeRequestRunCanceller(self.session)

    async def __aenter__(self) -> Self:
        return await _database(super().__aenter__())

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await _database(super().__aexit__(exc_type, exc, traceback))

    async def commit(self) -> None:
        await _database(super().commit())

    async def rollback(self) -> None:
        await _database(super().rollback())

    async def lock_target(self, target: BoundGitLabTarget) -> None:
        try:
            await self.session.execute(select(func.set_config("lock_timeout", self._timeout, True)))
            # Management's workspace fence and complete levels precede CodeChange/Run.
            statements = (
                select(Workspace.id).where(Workspace.id == target.workspace_id),
                select(ProviderInstallation.id).where(
                    ProviderInstallation.id == target.origin_provider_installation_id
                ),
                select(GitLabConnection.provider_installation_id).where(
                    GitLabConnection.provider_installation_id
                    == target.origin_provider_installation_id
                ),
                select(GitLabProjectBinding.repository_id).where(
                    GitLabProjectBinding.provider_installation_id
                    == target.origin_provider_installation_id,
                    GitLabProjectBinding.project_external_id == target.project_id,
                ),
                select(Repository.id).where(Repository.id == target.repository_id),
            )
            for statement in statements:
                if await self.session.scalar(statement.with_for_update()) is None:
                    raise GitLabTargetInactive()
        except GitLabTargetInactive:
            raise
        except Exception as error:
            _log_database_error(error)
            raise GitLabRequestUnavailable() from None
        await self._reader.checkpoint_in_session(self.session, target)
