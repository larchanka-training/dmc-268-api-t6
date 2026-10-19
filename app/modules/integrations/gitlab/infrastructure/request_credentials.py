"""Short detached SQL reads for one immutable GitLab origin/project binding."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.manage_connection import CredentialEnvelope
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.infrastructure.models import (
    GitLabConnection,
    GitLabProjectBinding,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.models import Workspace


def active_gitlab_target_metadata(
    *,
    require_repository_enabled: bool = True,
) -> Select[tuple[UUID, UUID, UUID, str, int, int]]:
    """Select safe metadata under the sole current GitLab binding authority.

    Callers may restrict identities or join this statement as a subquery, applying
    the same authority before their own ordered batch limit. No session is acquired.
    Delivery admission may include paused repositories so RunGuard can finish their
    already queued work; all other callers retain the strict enabled default.
    """
    statement = (
        select(
            GitLabConnection.provider_installation_id.label("origin_provider_installation_id"),
            GitLabConnection.workspace_id.label("workspace_id"),
            Repository.id.label("repository_id"),
            GitLabConnection.instance_origin.label("instance_origin"),
            GitLabProjectBinding.project_external_id.label("project_id"),
            GitLabConnection.credential_generation.label("credential_generation"),
        )
        .select_from(GitLabConnection)
        .join(
            ProviderInstallation,
            (ProviderInstallation.id == GitLabConnection.provider_installation_id)
            & (ProviderInstallation.workspace_id == GitLabConnection.workspace_id)
            & (ProviderInstallation.provider == GitLabConnection.provider),
        )
        .join(Workspace, Workspace.id == ProviderInstallation.workspace_id)
        .join(
            GitLabProjectBinding,
            (
                GitLabProjectBinding.provider_installation_id
                == GitLabConnection.provider_installation_id
            )
            & (GitLabProjectBinding.instance_origin == GitLabConnection.instance_origin),
        )
        .join(
            Repository,
            (Repository.id == GitLabProjectBinding.repository_id)
            & (Repository.provider_installation_id == GitLabConnection.provider_installation_id)
            & (Repository.external_id == GitLabProjectBinding.project_external_id),
        )
        .where(
            GitLabConnection.provider == "gitlab",
            ProviderInstallation.provider == "gitlab",
            ProviderInstallation.deleted_at.is_(None),
            GitLabConnection.revoked_at.is_(None),
            GitLabConnection.intake_enabled.is_(True),
            GitLabConnection.token_ciphertext.is_not(None),
            func.octet_length(GitLabConnection.token_ciphertext) > 0,
            GitLabConnection.credential_key_id.is_not(None),
            func.length(func.trim(GitLabConnection.credential_key_id)) > 0,
            GitLabConnection.credential_generation > 0,
            Repository.installation_available.is_(True),
        )
    )

    return (
        statement.where(Repository.enabled.is_(True)) if require_repository_enabled else statement
    )


class SqlAlchemyGitLabTargetReader:
    """No keys, digest, HTTP or environment configuration needed for metadata reads."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
        target, _ = await self._read(identity)
        return target

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        await self._pinned_read(target)

    async def checkpoint_in_session(self, session: AsyncSession, target: BoundGitLabTarget) -> None:
        """Borrow the caller's locked transaction; never acquire/close another session.

        The caller owns authority locks, commit, rollback and session lifetime. This
        metadata-only operation reuses the same active predicate as standalone reads.
        """
        await self._pinned_read(target, session=session)

    async def credential_envelope_for(self, target: BoundGitLabTarget) -> CredentialEnvelope:
        _, envelope = await self._pinned_read(target, include_credential=True)
        assert envelope is not None
        return envelope

    async def _pinned_read(
        self,
        target: BoundGitLabTarget,
        *,
        include_credential: bool = False,
        session: AsyncSession | None = None,
    ) -> tuple[BoundGitLabTarget, CredentialEnvelope | None]:
        if not isinstance(target, BoundGitLabTarget):
            raise ValueError("Invalid GitLab bound target")
        current, envelope = await self._read(
            GitLabTargetIdentity(target.origin_provider_installation_id, target.project_id),
            include_credential=include_credential,
            session=session,
        )
        if replace(current, credential_generation=target.credential_generation) != target:
            raise GitLabTargetInactive()
        if current.credential_generation != target.credential_generation:
            raise GitLabCredentialGenerationChanged()
        return current, envelope

    async def _read(
        self,
        identity: GitLabTargetIdentity,
        *,
        include_credential: bool = False,
        session: AsyncSession | None = None,
    ) -> tuple[BoundGitLabTarget, CredentialEnvelope | None]:
        if not isinstance(identity, GitLabTargetIdentity):
            raise ValueError("Invalid GitLab target identity")
        # Metadata paths never select ciphertext, key ID or webhook digest. The same
        # predicate authorizes envelope reads; no weaker eligibility algorithm exists.
        statement = active_gitlab_target_metadata().where(
            GitLabConnection.provider_installation_id == identity.origin_provider_installation_id,
            GitLabProjectBinding.project_external_id == identity.project_id,
        )
        if include_credential:
            statement = statement.add_columns(
                GitLabConnection.credential_key_id,
                GitLabConnection.token_ciphertext,
            )
        try:
            if session is None:
                async with self._sessions() as owned_session:
                    row = (await owned_session.execute(statement)).one_or_none()
            else:
                row = (await session.execute(statement)).one_or_none()
        except Exception:
            # Query/driver/connection failures are operational, never authority evidence.
            raise GitLabRequestUnavailable() from None
        if row is None:
            raise GitLabTargetInactive()
        try:
            target = BoundGitLabTarget(row[0], row[1], row[2], row[3], row[4], row[5])
        except ValueError:
            raise GitLabTargetInactive() from None
        envelope = CredentialEnvelope(row[6], row[7]) if include_credential else None
        return target, envelope
