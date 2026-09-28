"""Short PostgreSQL transaction for idempotent GitHub PR state projection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestIdentityConflict,
    PullRequestRecord,
    PullRequestState,
    RunCancellationNotice,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run


class SqlAlchemyPullRequestProjectionStore:
    """Resolve one installed repository, then upsert and lock its PR row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_locked(
        self, event: PullRequestEvent, now: datetime
    ) -> PullRequestRecord | None:
        repository_id = cast(
            UUID | None,
            await self._session.scalar(
                select(Repository.id)
                .join(
                    ProviderInstallation,
                    Repository.provider_installation_id == ProviderInstallation.id,
                )
                .where(
                    ProviderInstallation.provider == "github",
                    ProviderInstallation.external_id == event.installation_external_id,
                    Repository.external_id == event.repository_external_id,
                    Repository.enabled.is_(True),
                )
            ),
        )
        if repository_id is None:
            return None

        await self._session.execute(
            insert(CodeChange)
            .values(
                id=uuid4(),
                repository_id=repository_id,
                external_id=event.external_id,
                external_number=event.number,
                title=event.title,
                description=event.description,
                author_login=event.author_login,
                source_branch=event.source_branch,
                target_branch=event.target_branch,
                base_sha=event.base_sha,
                head_sha=event.head_sha,
                state=CodeChangeState(event.state.value),
                reviewer_requested=False,
                reviewer_requested_at=None,
                reviewer_intent_updated_at=None,
                reviewer_timeline_event_id=None,
                reviewer_timeline_position=None,
                reviewer_barrier_at=None,
                reviewer_barrier_position=None,
                head_first_seen_at=now,
                provider_updated_at=event.provider_updated_at,
                ci_status={},
                web_url=event.web_url,
            )
            .on_conflict_do_nothing()
        )
        row = await self._session.scalar(
            select(CodeChange)
            .where(
                CodeChange.repository_id == repository_id,
                CodeChange.external_number == event.number,
            )
            .with_for_update()
        )
        if row is None:
            raise PullRequestIdentityConflict
        return PullRequestRecord(
            id=row.id,
            repository_id=row.repository_id,
            external_id=row.external_id,
            external_number=row.external_number,
            title=row.title,
            description=row.description,
            author_login=row.author_login,
            web_url=row.web_url,
            source_branch=row.source_branch,
            target_branch=row.target_branch,
            base_sha=row.base_sha,
            head_sha=row.head_sha,
            state=PullRequestState(row.state.value),
            reviewer_requested=row.reviewer_requested,
            reviewer_requested_at=row.reviewer_requested_at,
            reviewer_intent_updated_at=row.reviewer_intent_updated_at,
            reviewer_timeline_event_id=row.reviewer_timeline_event_id,
            reviewer_timeline_position=row.reviewer_timeline_position,
            reviewer_barrier_at=row.reviewer_barrier_at,
            reviewer_barrier_position=row.reviewer_barrier_position,
            head_first_seen_at=row.head_first_seen_at,
            provider_updated_at=row.provider_updated_at,
            ci_status=dict(row.ci_status),
        )

    async def save(self, record: PullRequestRecord) -> None:
        await self._session.execute(
            update(CodeChange)
            .where(CodeChange.id == record.id)
            .values(
                title=record.title,
                description=record.description,
                author_login=record.author_login,
                web_url=record.web_url,
                source_branch=record.source_branch,
                target_branch=record.target_branch,
                base_sha=record.base_sha,
                head_sha=record.head_sha,
                state=CodeChangeState(record.state.value),
                reviewer_requested=record.reviewer_requested,
                reviewer_requested_at=record.reviewer_requested_at,
                reviewer_intent_updated_at=record.reviewer_intent_updated_at,
                reviewer_timeline_event_id=record.reviewer_timeline_event_id,
                reviewer_timeline_position=record.reviewer_timeline_position,
                reviewer_barrier_at=record.reviewer_barrier_at,
                reviewer_barrier_position=record.reviewer_barrier_position,
                head_first_seen_at=record.head_first_seen_at,
                provider_updated_at=record.provider_updated_at,
                ci_status=record.ci_status,
            )
        )


class SqlAlchemyPullRequestProjectionUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def pull_requests(self) -> SqlAlchemyPullRequestProjectionStore:
        return SqlAlchemyPullRequestProjectionStore(self.session)

    @property
    def runs(self) -> SqlAlchemyPullRequestRunCanceller:
        return SqlAlchemyPullRequestRunCanceller(self.session)


class SqlAlchemyPullRequestRunCanceller:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def cancel_for_pr(
        self, code_change_id: UUID, reason: str, now: datetime
    ) -> tuple[RunCancellationNotice, ...]:
        workspace_id = await self._session.scalar(
            select(ProviderInstallation.workspace_id)
            .join(Repository, Repository.provider_installation_id == ProviderInstallation.id)
            .join(CodeChange, CodeChange.repository_id == Repository.id)
            .where(CodeChange.id == code_change_id)
        )
        if workspace_id is None:
            raise RuntimeError("projected PR has no installation Workspace")
        runs = (
            await self._session.scalars(
                select(Run)
                .where(
                    Run.code_change_id == code_change_id,
                    Run.state.in_([RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING]),
                )
                .with_for_update()
            )
        ).all()
        notices: list[RunCancellationNotice] = []
        for run in runs:
            if run.state == RunState.QUEUED:
                run.state = RunState.CANCELLED
                run.error_code = reason
                run.finished_at = now
                notices.append(RunCancellationNotice(run.id, workspace_id, "cancelled"))
            elif not run.cancel_requested:
                run.cancel_requested = True
        await self._session.flush()
        return tuple(notices)

    async def notify_run_updated(self, notice: RunCancellationNotice) -> None:
        payload = json.dumps(
            {
                "run_id": str(notice.run_id),
                "workspace_id": str(notice.workspace_id),
                "status": notice.status,
            },
            separators=(",", ":"),
        )
        await self._session.execute(select(func.pg_notify("run_updated", payload)))


class SqlAlchemyPullRequestProjectionLock:
    """Session advisory lock across REST fetch and apply, on an autocommit connection."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def hold(self, event: PullRequestEvent) -> AsyncIterator[None]:
        identity = (
            f"{event.installation_external_id}:{event.repository_external_id}:{event.external_id}"
        )
        key = int.from_bytes(sha256(identity.encode()).digest()[:8], "big", signed=True)
        async with self._engine.connect() as connection:
            # AUTOCOMMIT keeps the session lock while no DB transaction spans GitHub I/O.
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                yield
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
