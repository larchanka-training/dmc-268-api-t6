"""T3 rerun persistence: one transaction owned by the ``RerunRun`` use case."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, Engine, RunState, SeverityThreshold
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.models import GitHubUserProfile
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RuleVersion,
)
from app.modules.reviews.application.queue_messages import StoredRunMessage
from app.modules.reviews.application.rerun_run import RerunFacts, RerunOriginFacts
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.run_notifications import notify_run_state
from app.modules.reviews.infrastructure.run_repository import authorized_run
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore

_ACTIVE = (RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING)


class SqlAlchemyRerunStore:
    """Flushes only; ``RerunRun`` commits."""

    def __init__(
        self,
        session: AsyncSession,
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyRerunStore requires an AuthScope unless allow_unscoped=True"
            )
        self._session = session
        self._scope = scope
        self._allow_unscoped = allow_unscoped

    async def load_rerun_facts(self, run_id: UUID) -> RerunFacts | None:
        original = (
            await self._session.execute(
                select(
                    Run.code_change_id,
                    Run.origin_provider_installation_id,
                    CodeChange.repository_id,
                    RuleVersion.repository_id,
                    ProviderInstallation.provider,
                    ProviderInstallation.workspace_id,
                    ProviderInstallation.account_external_id,
                    ProviderInstallation.deleted_at,
                )
                .join(CodeChange, CodeChange.id == Run.code_change_id)
                .join(RuleVersion, RuleVersion.id == Run.rule_version_id)
                .outerjoin(
                    ProviderInstallation,
                    ProviderInstallation.id == Run.origin_provider_installation_id,
                )
                .where(
                    Run.id == run_id,
                    authorized_run(self._scope, allow_unscoped=self._allow_unscoped),
                )
            )
        ).one_or_none()
        if original is None:
            return None
        (
            code_change_id,
            original_origin,
            original_repository,
            rule_repository,
            original_provider,
            original_workspace,
            original_account,
            original_deleted_at,
        ) = original
        # Match webhook/projection source-before-PR ordering before the active-Run decision.
        candidate = await SqlAlchemyWebhookRunStore(self._session).lock_candidate(code_change_id)
        pr_state = await self._session.scalar(
            select(CodeChange.state).where(CodeChange.id == code_change_id).with_for_update()
        )
        active = await self._session.scalar(
            select(Run.id).where(Run.code_change_id == code_change_id, Run.state.in_(_ACTIVE))
        )
        current = (
            await self._session.execute(
                select(
                    Repository.id,
                    ProviderInstallation.id,
                    ProviderInstallation.provider,
                    ProviderInstallation.workspace_id,
                    ProviderInstallation.account_external_id,
                    ProviderInstallation.deleted_at,
                )
                .select_from(CodeChange)
                .join(Repository, Repository.id == CodeChange.repository_id)
                .outerjoin(
                    ProviderInstallation,
                    ProviderInstallation.id == Repository.provider_installation_id,
                )
                .where(CodeChange.id == code_change_id)
            )
        ).one_or_none()
        return RerunFacts(
            code_change_id=code_change_id,
            original_repository_id=original_repository,
            rule_repository_id=rule_repository,
            origin=RerunOriginFacts(
                original_origin,
                original_provider,
                original_workspace,
                original_account,
                original_deleted_at is not None,
            ),
            candidate=candidate,
            current_origin=(
                RerunOriginFacts(
                    current[1], current[2], current[3], current[4], current[5] is not None
                )
                if current is not None and current[1] is not None
                else None
            ),
            current_repository_id=current[0] if current is not None else None,
            pull_request_open=pr_state == CodeChangeState.OPEN,
            active_run=active is not None,
        )

    async def insert_rerun(
        self, candidate: RunInsertCandidate, code_change_id: UUID, now: datetime
    ) -> PendingRunMessage:
        new_id = uuid4()
        login = await self._portal_login()
        self._session.add(
            Run(
                id=new_id,
                code_change_id=code_change_id,
                origin_provider_installation_id=candidate.origin_provider_installation_id,
                base_sha=candidate.base_sha,
                base_ref=candidate.base_ref,
                head_sha=candidate.ci.head_sha,
                state=RunState.QUEUED,
                trigger="rerun",
                idempotency_key=sha256(f"rerun:{new_id}".encode()).hexdigest(),
                engine=Engine(candidate.engine),
                rule_version_id=candidate.rule_version_id,
                prompt_version_id=candidate.prompt_version_id,
                severity_threshold=SeverityThreshold(candidate.severity_threshold),
                max_comments=candidate.max_comments,
                conventions_prompt_version_id=candidate.conventions_prompt_version_id,
                verifier_prompt_version_id=candidate.verifier_prompt_version_id,
                attempt=0,
                available_at=now,
                cancel_requested=False,
                message_published_at=None,
                created_at=now,
                triggered_by=login,
            )
        )
        await self._session.flush()
        await notify_run_state(self._session, new_id, RunState.QUEUED)
        pending = PendingRunMessage.from_candidate(new_id, candidate, now)
        return StoredRunMessage(**{**vars(pending), "trigger": "rerun"})

    async def _portal_login(self) -> str | None:
        if self._scope is None or self._allow_unscoped:
            return None
        login = await self._session.scalar(
            select(GitHubUserProfile.login).where(GitHubUserProfile.id == self._scope.user_id)
        )
        return login if isinstance(login, str) else None

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id, Run.message_published_at.is_(None))
            .values(message_published_at=now)
        )


class SqlAlchemyRerunUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyRerunUnitOfWork requires an AuthScope unless allow_unscoped=True"
            )
        super().__init__(session_factory)
        self._scope = scope
        self._allow_unscoped = allow_unscoped

    @property
    def runs(self) -> SqlAlchemyRerunStore:
        return SqlAlchemyRerunStore(self.session, self._scope, allow_unscoped=self._allow_unscoped)
