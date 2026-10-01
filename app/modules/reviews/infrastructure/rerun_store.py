"""T3 rerun persistence: one transaction owned by the ``RerunRun`` use case."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, Engine, RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.queue_messages import StoredRunMessage
from app.modules.reviews.application.rerun_run import RerunOutcome, RerunResult
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.run_notifications import notify_run_state
from app.modules.reviews.infrastructure.run_repository import authorized_run
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore

_ACTIVE = (RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING)


class SqlAlchemyRerunStore:
    """Flushes only; ``RerunRun`` commits."""

    def __init__(self, session: AsyncSession, scope: AuthScope | None) -> None:
        self._session = session
        self._scope = scope

    async def create_rerun(self, run_id: UUID, now: datetime) -> RerunResult:
        code_change_id = await self._session.scalar(
            select(Run.code_change_id).where(Run.id == run_id, authorized_run(self._scope))
        )
        if code_change_id is None:
            return RerunResult(RerunOutcome.NOT_FOUND)
        # The PR row lock is the one the webhook path takes: one active-Run decision.
        pr_state = await self._session.scalar(
            select(CodeChange.state).where(CodeChange.id == code_change_id).with_for_update()
        )
        active = await self._session.scalar(
            select(Run.id).where(Run.code_change_id == code_change_id, Run.state.in_(_ACTIVE))
        )
        if pr_state != CodeChangeState.OPEN or active is not None:
            return RerunResult(RerunOutcome.CONFLICT)
        candidate = await SqlAlchemyWebhookRunStore(self._session).lock_candidate(code_change_id)
        if candidate is None:
            # No active rule or prompt version, or no GitHub installation: not a T3 conflict.
            return RerunResult(RerunOutcome.NOT_CONFIGURED)
        new_id = uuid4()
        self._session.add(
            Run(
                id=new_id,
                code_change_id=code_change_id,
                base_sha=candidate.base_sha,
                base_ref=candidate.base_ref,
                head_sha=candidate.ci.head_sha,
                state=RunState.QUEUED,
                trigger="rerun",
                idempotency_key=sha256(f"rerun:{new_id}".encode()).hexdigest(),
                engine=Engine(candidate.engine),
                rule_version_id=candidate.rule_version_id,
                prompt_version_id=candidate.prompt_version_id,
                attempt=0,
                available_at=now,
                cancel_requested=False,
                message_published_at=None,
                created_at=now,
            )
        )
        await self._session.flush()
        await notify_run_state(self._session, new_id, RunState.QUEUED)
        pending = PendingRunMessage.from_candidate(new_id, candidate, now)
        return RerunResult(
            RerunOutcome.CREATED, StoredRunMessage(**{**vars(pending), "trigger": "rerun"})
        )

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id, Run.message_published_at.is_(None))
            .values(message_published_at=now)
        )


class SqlAlchemyRerunUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], scope: AuthScope | None = None
    ) -> None:
        super().__init__(session_factory)
        self._scope = scope

    @property
    def runs(self) -> SqlAlchemyRerunStore:
        return SqlAlchemyRerunStore(self.session, self._scope)
