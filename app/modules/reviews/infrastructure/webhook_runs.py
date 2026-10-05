"""Short transactional Run insert and notification for verified webhook triggers."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RuleVersion,
)
from app.modules.reviews.application.determine_ci_eligibility import (
    CiWaitMode,
    EligibilityCandidate,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.application.try_enqueue_webhook_run import (
    CandidateMiss,
    DuplicateReason,
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.models import CodeChange, PromptVersion, Run


class SqlAlchemyWebhookRunStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate | CandidateMiss:
        pr = await self._session.scalar(
            select(CodeChange).where(CodeChange.id == code_change_id).with_for_update()
        )
        if pr is None:
            return CandidateMiss.PULL_REQUEST_GONE
        repository = await self._session.get(Repository, pr.repository_id)
        if repository is None:
            return CandidateMiss.REPOSITORY_GONE
        installation = await self._session.get(
            ProviderInstallation, repository.provider_installation_id
        )
        if installation is None or installation.provider != "github":
            return CandidateMiss.MISSING_INSTALLATION
        rule_version_id = await self._session.scalar(
            select(RuleVersion.id).where(
                RuleVersion.repository_id == repository.id, RuleVersion.is_active.is_(True)
            )
        )
        if rule_version_id is None:
            return CandidateMiss.MISSING_RULES
        prompt_version_id = repository.prompt_version_id
        if prompt_version_id is None:
            prompt_version_id = await self._session.scalar(
                select(PromptVersion.id).where(
                    PromptVersion.key == "review.system", PromptVersion.is_active.is_(True)
                )
            )
        if prompt_version_id is None:
            return CandidateMiss.MISSING_PROMPT
        return RunInsertCandidate(
            ci=EligibilityCandidate(
                code_change_id=pr.id,
                installation_external_id=installation.external_id,
                repository_full_name=repository.full_name,
                head_sha=pr.head_sha,
                state=PullRequestState(pr.state.value),
                repository_enabled=repository.enabled,
                ai_review_labeled=pr.ai_review_labeled,
                ai_review_labeled_at=pr.ai_review_labeled_at,
                head_first_seen_at=pr.head_first_seen_at,
                wait_for_ci=CiWaitMode(repository.wait_for_ci.value),
            ),
            repository_id=repository.id,
            workspace_id=installation.workspace_id,
            repository_external_id=repository.external_id,
            pr_number=pr.external_number,
            base_sha=pr.base_sha,
            base_ref=pr.target_branch,
            engine=repository.default_engine.value,
            rule_version_id=rule_version_id,
            prompt_version_id=prompt_version_id,
        )

    async def insert_webhook_run(
        self, candidate: RunInsertCandidate, now: datetime
    ) -> PendingRunMessage | DuplicateReason:
        code_change_id = candidate.ci.code_change_id
        active = await self._session.scalar(
            select(Run.id).where(
                Run.code_change_id == code_change_id,
                Run.state.in_([RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING]),
            )
        )
        if active is not None:
            return DuplicateReason.ACTIVE_RUN
        idempotency_key = sha256(
            f"webhook:{code_change_id}:{candidate.ci.head_sha}".encode()
        ).hexdigest()
        run_id = uuid4()
        inserted = await self._session.scalar(
            insert(Run)
            .values(
                id=run_id,
                code_change_id=code_change_id,
                base_sha=candidate.base_sha,
                base_ref=candidate.base_ref,
                head_sha=candidate.ci.head_sha,
                state=RunState.QUEUED,
                trigger="webhook",
                idempotency_key=idempotency_key,
                engine=candidate.engine,
                rule_version_id=candidate.rule_version_id,
                prompt_version_id=candidate.prompt_version_id,
                attempt=0,
                available_at=now,
                cancel_requested=False,
                message_published_at=None,
                created_at=now,
            )
            .on_conflict_do_nothing()
            .returning(Run.id)
        )
        if inserted is None:
            return DuplicateReason.HEAD_ALREADY_REVIEWED
        return PendingRunMessage.from_candidate(inserted, candidate, now)

    async def notify_run_updated(self, run_id: UUID, workspace_id: UUID, status: str) -> None:
        payload = json.dumps(
            {"run_id": str(run_id), "workspace_id": str(workspace_id), "status": status},
            separators=(",", ":"),
        )
        await self._session.execute(select(func.pg_notify("run_updated", payload)))

    async def mark_published(self, run_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id, Run.trigger == "webhook", Run.message_published_at.is_(None))
            .values(message_published_at=now)
        )

    async def pending_messages(self, limit: int) -> tuple[PendingRunMessage, ...]:
        rows = (
            await self._session.execute(
                select(Run, CodeChange, Repository, ProviderInstallation)
                .join(CodeChange, Run.code_change_id == CodeChange.id)
                .join(Repository, CodeChange.repository_id == Repository.id)
                .join(
                    ProviderInstallation,
                    Repository.provider_installation_id == ProviderInstallation.id,
                )
                .where(
                    Run.trigger == "webhook",
                    Run.state == RunState.QUEUED,
                    Run.message_published_at.is_(None),
                    Run.base_ref.is_not(None),
                    Run.cancel_requested.is_(False),
                    CodeChange.state == CodeChangeState.OPEN,
                    CodeChange.head_sha == Run.head_sha,
                    CodeChange.ai_review_labeled.is_(True),
                    Repository.enabled.is_(True),
                    ProviderInstallation.provider == "github",
                )
                .order_by(Run.created_at, Run.id)
                .limit(limit)
            )
        ).all()
        return tuple(
            PendingRunMessage(
                run_id=run.id,
                workspace_id=installation.workspace_id,
                installation_id=installation.external_id,
                repository_id=repository.id,
                repository_external_id=repository.external_id,
                repository_full_name=repository.full_name,
                pr_number=pr.external_number,
                head_sha=run.head_sha,
                base_sha=run.base_sha,
                base_ref=run.base_ref or pr.target_branch,
                engine=run.engine.value,
                rule_version_id=run.rule_version_id,
                prompt_version_id=run.prompt_version_id,
                attempt=run.attempt + 1,
                requested_at=run.created_at,
            )
            for run, pr, repository, installation in rows
        )


class SqlAlchemyWebhookRunUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def runs(self) -> SqlAlchemyWebhookRunStore:
        return SqlAlchemyWebhookRunStore(self.session)
