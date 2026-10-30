"""Short Bitbucket PR projection transactions under immutable binding authority."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, RunState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketTargetInactive,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketInstallationIdentity,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestCommand,
    BitbucketPullRequestIdentityMismatch,
    BitbucketPullRequestStale,
    BitbucketPullRequestUntrackedClosed,
    CanonicalBitbucketPullRequest,
    ProjectedBitbucketPullRequest,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from app.modules.workspaces.infrastructure.models import Workspace


async def _active_target(
    session: AsyncSession,
    *,
    installation_id: UUID,
    repository_id: UUID,
    repository_uuid: UUID,
) -> tuple[UUID, int, int, int, UUID] | None:
    row = (
        await session.execute(
            select(
                ProviderInstallation.workspace_id,
                BitbucketConnection.credential_generation,
                BitbucketRepositoryWebhookSecret.binding_generation,
                BitbucketRepositoryWebhookSecret.generation,
                BitbucketInstallationIdentity.bitbucket_workspace_uuid,
            )
            .select_from(ProviderInstallation)
            .join(
                BitbucketConnection,
                BitbucketConnection.provider_installation_id == ProviderInstallation.id,
            )
            .join(
                BitbucketInstallationIdentity,
                BitbucketInstallationIdentity.provider_installation_id == ProviderInstallation.id,
            )
            .join(Repository, Repository.provider_installation_id == ProviderInstallation.id)
            .join(
                BitbucketRepositoryIdentity,
                BitbucketRepositoryIdentity.repository_id == Repository.id,
            )
            .join(
                BitbucketRepositoryWebhookSecret,
                BitbucketRepositoryWebhookSecret.repository_id == Repository.id,
            )
            .where(
                ProviderInstallation.id == installation_id,
                ProviderInstallation.provider == "bitbucket",
                ProviderInstallation.deleted_at.is_(None),
                BitbucketConnection.workspace_id == ProviderInstallation.workspace_id,
                BitbucketInstallationIdentity.workspace_id == ProviderInstallation.workspace_id,
                BitbucketConnection.revoked_at.is_(None),
                BitbucketConnection.token_ciphertext.is_not(None),
                Repository.id == repository_id,
                Repository.enabled.is_(True),
                Repository.installation_available.is_(True),
                BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                BitbucketRepositoryIdentity.workspace_id == ProviderInstallation.workspace_id,
                BitbucketRepositoryIdentity.repository_uuid == repository_uuid,
                BitbucketRepositoryWebhookSecret.secret_ciphertext.is_not(None),
                BitbucketRepositoryWebhookSecret.revoked_at.is_(None),
                BitbucketRepositoryWebhookSecret.confirmed_at.is_not(None),
            )
        )
    ).one_or_none()
    return tuple(row) if row is not None else None


class SqlAlchemyBitbucketProjectionAuthority:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def resolve(self, command: BitbucketPullRequestCommand) -> BoundBitbucketTarget:
        async with self._sessions() as session:
            row = await _active_target(
                session,
                installation_id=command.installation_id,
                repository_id=command.repository_id,
                repository_uuid=command.repository_uuid,
            )
        if row is None:
            raise BitbucketTargetInactive()
        workspace, credential, binding, webhook, remote_workspace = row
        if binding != command.binding_generation or webhook != command.webhook_generation:
            raise BitbucketBindingGenerationChanged()
        return BoundBitbucketTarget(
            command.installation_id,
            workspace,
            command.repository_id,
            command.repository_uuid,
            credential,
            binding,
            remote_workspace,
        )

    async def checkpoint(self, target: BoundBitbucketTarget, webhook_generation: int) -> None:
        async with self._sessions() as session:
            await _check_target(session, target, webhook_generation)


async def _check_target(
    session: AsyncSession, target: BoundBitbucketTarget, webhook_generation: int
) -> None:
    row = await _active_target(
        session,
        installation_id=target.installation_id,
        repository_id=target.repository_id,
        repository_uuid=target.repository_uuid,
    )
    if row is None or row[0] != target.workspace_id:
        raise BitbucketTargetInactive()
    if target.bitbucket_workspace_uuid is not None and row[4] != target.bitbucket_workspace_uuid:
        raise BitbucketTargetInactive()
    if row[1] != target.credential_generation:
        raise BitbucketCredentialGenerationChanged()
    if row[2] != target.binding_generation or row[3] != webhook_generation:
        raise BitbucketBindingGenerationChanged()


class SqlAlchemyBitbucketPushCandidateReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def matching_open_pr_numbers(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        binding_generation: int,
        webhook_generation: int,
        branches: frozenset[str],
    ) -> tuple[int, ...]:
        if not branches:
            return ()
        async with self._sessions() as session:
            row = await _active_target(
                session,
                installation_id=installation_id,
                repository_id=repository_id,
                repository_uuid=repository_uuid,
            )
            if row is None:
                raise BitbucketTargetInactive()
            if row[2] != binding_generation or row[3] != webhook_generation:
                raise BitbucketBindingGenerationChanged()
            numbers = tuple(
                await session.scalars(
                    select(CodeChange.external_number)
                    .where(
                        CodeChange.repository_id == repository_id,
                        CodeChange.state == CodeChangeState.OPEN,
                        CodeChange.source_branch.in_(branches),
                    )
                    .order_by(CodeChange.external_number)
                    .limit(501)
                )
            )
        if len(numbers) > 500:
            raise RuntimeError("bitbucket_push_candidate_limit")
        return numbers


class SqlAlchemyBitbucketPullRequestProjectionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def project(
        self,
        target: BoundBitbucketTarget,
        webhook_generation: int,
        current: CanonicalBitbucketPullRequest,
    ) -> ProjectedBitbucketPullRequest:
        await _check_target(self._session, target, webhook_generation)
        if current.repository_uuid != target.repository_uuid:
            raise BitbucketPullRequestIdentityMismatch()
        lookup = (
            select(CodeChange)
            .where(
                CodeChange.repository_id == target.repository_id,
                (CodeChange.external_id == current.number)
                | (CodeChange.external_number == current.number),
            )
            .order_by(CodeChange.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing: tuple[CodeChange, ...] = ()
        if current.head_sha is None:
            if current.state == "open":
                raise BitbucketPullRequestIdentityMismatch()
            existing = tuple(await self._session.scalars(lookup))
            if not existing:
                raise BitbucketPullRequestUntrackedClosed()
            if (
                len(existing) != 1
                or existing[0].external_id != current.number
                or existing[0].external_number != current.number
            ):
                raise BitbucketPullRequestStale()
            head_sha = existing[0].head_sha
            base_sha = existing[0].base_sha
        else:
            head_sha = current.head_sha
            base_sha = current.base_sha
        values = dict(
            repository_id=target.repository_id,
            external_id=current.number,
            external_number=current.number,
            title=current.title,
            description=current.description,
            author_login=current.author_login,
            source_branch=current.source_branch,
            target_branch=current.target_branch,
            base_sha=base_sha,
            head_sha=head_sha,
            state=CodeChangeState(current.state),
            provider_updated_at=current.provider_updated_at,
            web_url=current.web_url,
        )
        now = datetime.now(UTC)
        inserted = None
        if not existing:
            inserted = await self._session.scalar(
                insert(CodeChange)
                .values(id=uuid4(), head_first_seen_at=now, **values)
                .on_conflict_do_nothing()
                .returning(CodeChange.id)
            )
        rows = existing or tuple(await self._session.scalars(lookup))
        if (
            len(rows) != 1
            or rows[0].external_id != current.number
            or rows[0].external_number != current.number
        ):
            raise BitbucketPullRequestIdentityMismatch()
        row = rows[0]
        if row.provider_updated_at is not None and (
            current.provider_updated_at < row.provider_updated_at
            or (current.provider_updated_at == row.provider_updated_at and head_sha != row.head_sha)
        ):
            raise BitbucketPullRequestStale()
        changed = inserted is not None or row.head_sha != head_sha
        if changed:
            row.head_first_seen_at = now
        for key, value in values.items():
            setattr(row, key, value)
        if changed or current.state != "open":
            reason = "superseded" if current.state == "open" else "closed"
            await self._cancel_obsolete(row.id, target.workspace_id, head_sha, reason, now)
        await self._session.flush()
        external_id = await self._session.scalar(
            select(Repository.external_id).where(Repository.id == target.repository_id)
        )
        if external_id is None:
            raise BitbucketTargetInactive()
        # Projection and Run insertion commit in separate transactions. A retry
        # must repair a crash or deferred insert after this PR head was committed.
        run_missing = (
            await self._session.scalar(
                select(Run.id).where(
                    Run.code_change_id == row.id,
                    Run.head_sha == head_sha,
                    Run.trigger == "webhook",
                )
            )
            is None
        )
        return ProjectedBitbucketPullRequest(
            row.id, external_id, head_sha, current.state, changed, run_missing
        )

    async def _cancel_obsolete(
        self, code_change_id: UUID, workspace_id: UUID, head_sha: str, reason: str, now: datetime
    ) -> None:
        conditions = [
            Run.code_change_id == code_change_id,
            Run.state.in_((RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING)),
        ]
        if reason == "superseded":
            conditions.append(Run.head_sha != head_sha)
        runs = tuple(
            await self._session.scalars(
                select(Run).where(*conditions).order_by(Run.id).with_for_update()
            )
        )
        for run in runs:
            if run.state == RunState.QUEUED:
                run.state = RunState.CANCELLED
                run.error_code = reason
                run.finished_at = now
                if run.attempt >= 1 and run.cancellation_signal_requested_at is None:
                    run.cancellation_signal_requested_at = now
                await SqlAlchemyWebhookRunStore(self._session).notify_run_updated(
                    run.id, workspace_id, "cancelled"
                )
            elif not run.cancel_requested:
                run.cancel_requested = True


class SqlAlchemyBitbucketProjectionUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def pull_requests(self) -> SqlAlchemyBitbucketPullRequestProjectionStore:
        return SqlAlchemyBitbucketPullRequestProjectionStore(self.session)

    async def lock_target(self, target: BoundBitbucketTarget, webhook_generation: int) -> None:
        # Management locks workspace, installation, credential, repository, binding.
        keys = (
            select(Workspace.id).where(Workspace.id == target.workspace_id),
            select(ProviderInstallation.id).where(
                ProviderInstallation.id == target.installation_id,
                ProviderInstallation.workspace_id == target.workspace_id,
            ),
            select(BitbucketConnection.provider_installation_id).where(
                BitbucketConnection.provider_installation_id == target.installation_id,
                BitbucketConnection.workspace_id == target.workspace_id,
            ),
            select(Repository.id).where(
                Repository.id == target.repository_id,
                Repository.provider_installation_id == target.installation_id,
            ),
            select(BitbucketRepositoryWebhookSecret.repository_id).where(
                BitbucketRepositoryWebhookSecret.repository_id == target.repository_id
            ),
        )
        for statement in keys:
            if await self.session.scalar(statement.with_for_update()) is None:
                raise BitbucketTargetInactive()
        await _check_target(self.session, target, webhook_generation)
