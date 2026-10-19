"""Short transactional Run insert and notification for verified webhook triggers."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, RunState, WaitForCi
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.gitlab.application.request_credentials import GitLabRequestUnavailable
from app.modules.integrations.gitlab.infrastructure.models import (
    GitLabConnection,
    GitLabProjectBinding,
)
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    active_gitlab_target_metadata,
)
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RuleVersion,
)
from app.modules.reviews.application.determine_ci_eligibility import (
    CiWaitMode,
    EligibilityCandidate,
    WebhookRunIdentity,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.application.try_enqueue_webhook_run import (
    ActiveRunBlocker,
    CandidateMiss,
    DuplicateReason,
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.gitlab_eligibility_candidates import (
    read_gitlab_eligibility_candidate,
    read_gitlab_eligibility_target,
)
from app.modules.reviews.infrastructure.models import CodeChange, PromptVersion, Run
from app.modules.reviews.infrastructure.run_origin import active_origin_condition
from app.modules.workspaces.infrastructure.models import Workspace


class SqlAlchemyWebhookRunStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate | CandidateMiss:
        # A nonlocking provider route is advisory; each branch rechecks under locks.
        try:
            provider = await self._session.scalar(
                select(ProviderInstallation.provider)
                .join(Repository, Repository.provider_installation_id == ProviderInstallation.id)
                .join(CodeChange, CodeChange.repository_id == Repository.id)
                .where(CodeChange.id == code_change_id)
            )
        except Exception:
            raise GitLabRequestUnavailable() from None
        gitlab_ci: EligibilityCandidate | None = None
        installation: ProviderInstallation | None
        pr: CodeChange | None
        repository: Repository | None
        if provider == "gitlab":
            locked = await self._lock_gitlab(code_change_id)
            if isinstance(locked, CandidateMiss):
                return locked
            gitlab_ci, pr, repository, installation = locked
        else:
            # Installation locks precede PR/Run locks, matching retirement and OAuth apply.
            installation = await self._session.scalar(
                select(ProviderInstallation)
                .join(Repository, Repository.provider_installation_id == ProviderInstallation.id)
                .join(CodeChange, CodeChange.repository_id == Repository.id)
                .where(CodeChange.id == code_change_id)
                .with_for_update(of=ProviderInstallation, key_share=True)
                .execution_options(populate_existing=True)
            )
            pr = await self._session.scalar(
                select(CodeChange).where(CodeChange.id == code_change_id).with_for_update()
            )
            if pr is None:
                return CandidateMiss.PULL_REQUEST_GONE
            repository = await self._session.get(Repository, pr.repository_id)
            if repository is None:
                return CandidateMiss.REPOSITORY_GONE
            if (
                installation is None
                or installation.provider != "github"
                or installation.id != repository.provider_installation_id
            ):
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
            ci=gitlab_ci
            or EligibilityCandidate(
                code_change_id=pr.id,
                installation_external_id=installation.external_id,
                repository_full_name=repository.full_name,
                head_sha=pr.head_sha,
                state=PullRequestState(pr.state.value),
                repository_enabled=(
                    repository.enabled
                    and repository.installation_available
                    and installation.deleted_at is None
                ),
                ai_review_labeled=pr.ai_review_labeled,
                ai_review_labeled_at=pr.ai_review_labeled_at,
                head_first_seen_at=pr.head_first_seen_at,
                wait_for_ci=CiWaitMode(repository.wait_for_ci.value),
                identity=WebhookRunIdentity(
                    provider="github",
                    origin_provider_installation_id=installation.id,
                    workspace_id=installation.workspace_id,
                    repository_id=repository.id,
                    repository_external_id=repository.external_id,
                ),
            ),
            origin_provider_installation_id=installation.id,
            repository_id=repository.id,
            workspace_id=installation.workspace_id,
            repository_external_id=repository.external_id,
            pr_number=pr.external_number,
            base_sha=pr.base_sha,
            base_ref=pr.target_branch,
            engine=repository.default_engine.value,
            rule_version_id=rule_version_id,
            prompt_version_id=prompt_version_id,
            provider="gitlab" if gitlab_ci is not None else "github",
        )

    async def _lock_gitlab(
        self, code_change_id: UUID
    ) -> tuple[EligibilityCandidate, CodeChange, Repository, ProviderInstallation] | CandidateMiss:
        target = await read_gitlab_eligibility_target(self._session, code_change_id)
        if target is None:
            return CandidateMiss.MISSING_INSTALLATION
        # Same complete authority levels/order as management and projection. Never
        # change the original target to the current replacement after waiting.
        statements = (
            select(Workspace.id).where(Workspace.id == target.workspace_id),
            select(ProviderInstallation.id).where(
                ProviderInstallation.id == target.origin_provider_installation_id
            ),
            select(GitLabConnection.provider_installation_id).where(
                GitLabConnection.provider_installation_id == target.origin_provider_installation_id
            ),
            select(GitLabProjectBinding.repository_id).where(
                GitLabProjectBinding.provider_installation_id
                == target.origin_provider_installation_id,
                GitLabProjectBinding.project_external_id == target.project_id,
            ),
            select(Repository.id).where(Repository.id == target.repository_id),
        )
        try:
            for statement in statements:
                if await self._session.scalar(statement.with_for_update()) is None:
                    return CandidateMiss.MISSING_INSTALLATION
            pr = await self._session.scalar(
                select(CodeChange)
                .where(CodeChange.id == code_change_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if pr is None:
                return CandidateMiss.PULL_REQUEST_GONE
            repository = await self._session.scalar(
                select(Repository)
                .where(Repository.id == target.repository_id)
                .execution_options(populate_existing=True)
            )
            installation = await self._session.scalar(
                select(ProviderInstallation)
                .where(ProviderInstallation.id == target.origin_provider_installation_id)
                .execution_options(populate_existing=True)
            )
        except Exception:
            raise GitLabRequestUnavailable() from None
        # Shared metadata mapper invokes ONLY T8's borrowed same-session checkpoint.
        ci = await read_gitlab_eligibility_candidate(
            self._session, code_change_id, expected_target=target
        )
        if ci is None or installation is None or repository is None:
            return CandidateMiss.MISSING_INSTALLATION
        return ci, pr, repository, installation

    async def insert_webhook_run(
        self, candidate: RunInsertCandidate, now: datetime
    ) -> PendingRunMessage | DuplicateReason | ActiveRunBlocker:
        code_change_id = candidate.ci.code_change_id
        active = await self._session.scalar(
            select(Run).where(
                Run.code_change_id == code_change_id,
                Run.state.in_([RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING]),
            )
        )
        if active is not None:
            return ActiveRunBlocker(active.head_sha, active.cancel_requested)
        idempotency_key = sha256(
            f"webhook:{code_change_id}:{candidate.ci.head_sha}".encode()
        ).hexdigest()
        run_id = uuid4()
        inserted = await self._session.scalar(
            insert(Run)
            .values(
                id=run_id,
                code_change_id=code_change_id,
                origin_provider_installation_id=candidate.origin_provider_installation_id,
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
            # ``head_already_reviewed`` is inferred from any unique violation here. That is safe
            # only because the PR row lock and the active-Run pre-check above make
            # ``uq_runs_one_active_per_code_change`` unreachable: what is left to collide is
            # this head's idempotency key / ``uq_runs_webhook_code_change_head``.
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
        gitlab = active_gitlab_target_metadata().subquery()
        rows = (
            await self._session.execute(
                select(Run, CodeChange, Repository, ProviderInstallation)
                .join(CodeChange, Run.code_change_id == CodeChange.id)
                .join(Repository, CodeChange.repository_id == Repository.id)
                .join(
                    ProviderInstallation,
                    Run.origin_provider_installation_id == ProviderInstallation.id,
                )
                .outerjoin(
                    gitlab,
                    (gitlab.c.origin_provider_installation_id == ProviderInstallation.id)
                    & (gitlab.c.workspace_id == ProviderInstallation.workspace_id)
                    & (gitlab.c.repository_id == Repository.id)
                    & (gitlab.c.project_id == Repository.external_id),
                )
                .where(
                    active_origin_condition(),
                    Run.trigger == "webhook",
                    Run.state == RunState.QUEUED,
                    Run.message_published_at.is_(None),
                    Run.base_ref.is_not(None),
                    Run.cancel_requested.is_(False),
                    CodeChange.state == CodeChangeState.OPEN,
                    CodeChange.head_sha == Run.head_sha,
                    Repository.enabled.is_(True),
                    Repository.installation_available.is_(True),
                    or_(
                        and_(
                            ProviderInstallation.provider == "github",
                            CodeChange.ai_review_labeled.is_(True),
                        ),
                        and_(
                            ProviderInstallation.provider == "gitlab",
                            gitlab.c.origin_provider_installation_id.is_not(None),
                            Repository.wait_for_ci == WaitForCi.NEVER,
                        ),
                    ),
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
                provider=installation.provider,
            )
            for run, pr, repository, installation in rows
        )


class SqlAlchemyWebhookRunUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def runs(self) -> SqlAlchemyWebhookRunStore:
        return SqlAlchemyWebhookRunStore(self.session)
