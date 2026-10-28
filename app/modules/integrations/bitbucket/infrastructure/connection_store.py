"""Flush-only persistence for Bitbucket credentials and portal ownership."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import WaitForCi
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketManagementError,
    EncryptedCredentialLike,
    InstallationSnapshot,
    RepositoryBinding,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketInstallationIdentity,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
    bitbucket_installation_external_id_seq,
    bitbucket_repository_external_id_seq,
)
from app.modules.repositories.application.onboard_repository import PersistedRuleVersion
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.repositories.infrastructure.rule_version_repository import (
    SqlAlchemyRepositoryRuleVersionStore,
)
from app.modules.repositories.infrastructure.rules_status import request_rules_refresh
from app.modules.workspaces.infrastructure.models import GitHubUserWorkspaceAccess, Workspace


class SqlAlchemyBitbucketConnectionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._rules = SqlAlchemyRepositoryRuleVersionStore(session)

    async def is_member(self, *, user_id: int, workspace_id: UUID, lock: bool = False) -> bool:
        statement = (
            select(Workspace.id)
            .join(
                GitHubUserWorkspaceAccess,
                GitHubUserWorkspaceAccess.workspace_id == Workspace.id,
            )
            .where(
                Workspace.id == workspace_id,
                GitHubUserWorkspaceAccess.github_user_id == user_id,
            )
        )
        if lock:
            statement = statement.with_for_update(of=(Workspace, GitHubUserWorkspaceAccess))
        return await self._session.scalar(statement) is not None

    async def installation_for_remote_workspace(
        self, *, workspace_id: UUID, bitbucket_workspace_uuid: UUID
    ) -> InstallationSnapshot | None:
        row = (
            await self._session.execute(
                select(
                    ProviderInstallation.id,
                    BitbucketConnection.credential_generation,
                    BitbucketInstallationIdentity.bitbucket_workspace_uuid,
                    BitbucketConnection.token_key_id,
                    BitbucketConnection.token_ciphertext,
                )
                .select_from(BitbucketInstallationIdentity)
                .join(
                    ProviderInstallation,
                    ProviderInstallation.id
                    == BitbucketInstallationIdentity.provider_installation_id,
                )
                .join(
                    BitbucketConnection,
                    BitbucketConnection.provider_installation_id == ProviderInstallation.id,
                )
                .where(
                    BitbucketInstallationIdentity.workspace_id == workspace_id,
                    BitbucketInstallationIdentity.bitbucket_workspace_uuid
                    == bitbucket_workspace_uuid,
                    ProviderInstallation.workspace_id == workspace_id,
                    ProviderInstallation.provider == "bitbucket",
                    ProviderInstallation.deleted_at.is_(None),
                    BitbucketConnection.workspace_id == workspace_id,
                    BitbucketConnection.revoked_at.is_(None),
                )
                .with_for_update(of=(ProviderInstallation, BitbucketConnection))
            )
        ).one_or_none()
        if row is None or row[3] is None or row[4] is None:
            return None
        return InstallationSnapshot(row[0], row[1], row[2], row[3], row[4])

    async def installation_by_id(
        self, *, workspace_id: UUID, installation_id: UUID
    ) -> InstallationSnapshot | None:
        parent, connection = await self._active_connection(workspace_id, installation_id)
        if (
            parent is None
            or connection is None
            or connection.token_key_id is None
            or connection.token_ciphertext is None
        ):
            return None
        workspace_uuid = await self._session.scalar(
            select(BitbucketInstallationIdentity.bitbucket_workspace_uuid).where(
                BitbucketInstallationIdentity.provider_installation_id == installation_id,
                BitbucketInstallationIdentity.workspace_id == workspace_id,
            )
        )
        if workspace_uuid is None:
            return None
        return InstallationSnapshot(
            installation_id,
            connection.credential_generation,
            workspace_uuid,
            connection.token_key_id,
            connection.token_ciphertext,
        )

    async def revoked_installation_for_remote_workspace(
        self, *, workspace_id: UUID, bitbucket_workspace_uuid: UUID
    ) -> UUID | None:
        result: UUID | None = await self._session.scalar(
            select(ProviderInstallation.id)
            .select_from(BitbucketInstallationIdentity)
            .join(
                ProviderInstallation,
                ProviderInstallation.id == BitbucketInstallationIdentity.provider_installation_id,
            )
            .join(
                BitbucketConnection,
                BitbucketConnection.provider_installation_id == ProviderInstallation.id,
            )
            .where(
                BitbucketInstallationIdentity.workspace_id == workspace_id,
                BitbucketInstallationIdentity.bitbucket_workspace_uuid == bitbucket_workspace_uuid,
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.provider == "bitbucket",
                ProviderInstallation.deleted_at.is_not(None),
                BitbucketConnection.revoked_at.is_not(None),
            )
            .with_for_update(of=(ProviderInstallation, BitbucketConnection))
        )
        return result

    async def reopen_connection(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        token: EncryptedCredentialLike,
    ) -> int:
        parent = await self._session.scalar(
            select(ProviderInstallation)
            .where(
                ProviderInstallation.id == installation_id,
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.provider == "bitbucket",
            )
            .with_for_update()
        )
        connection = await self._session.scalar(
            select(BitbucketConnection)
            .where(
                BitbucketConnection.provider_installation_id == installation_id,
                BitbucketConnection.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if (
            parent is None
            or connection is None
            or parent.deleted_at is None
            or connection.revoked_at is None
        ):
            raise BitbucketManagementError("bitbucket_connection_not_revoked")
        parent.deleted_at = None
        connection.revoked_at = None
        connection.token_ciphertext = token.ciphertext
        connection.token_key_id = token.key_id
        connection.credential_generation += 1
        connection.intake_enabled = False
        await self._session.flush()
        return connection.credential_generation

    async def list_bindings(self, *, workspace_id: UUID) -> tuple[RepositoryBinding, ...]:
        rows = (
            await self._session.execute(
                select(
                    ProviderInstallation.id,
                    Repository.id,
                    BitbucketRepositoryIdentity.repository_uuid,
                    Repository.full_name,
                    Repository.web_url,
                    BitbucketConnection.credential_generation,
                    BitbucketRepositoryWebhookSecret.binding_generation,
                    BitbucketRepositoryWebhookSecret.confirmed_at,
                )
                .select_from(BitbucketRepositoryIdentity)
                .join(Repository, Repository.id == BitbucketRepositoryIdentity.repository_id)
                .join(
                    BitbucketRepositoryWebhookSecret,
                    BitbucketRepositoryWebhookSecret.repository_id == Repository.id,
                )
                .join(
                    ProviderInstallation,
                    ProviderInstallation.id == Repository.provider_installation_id,
                )
                .join(
                    BitbucketConnection,
                    BitbucketConnection.provider_installation_id == ProviderInstallation.id,
                )
                .where(
                    BitbucketRepositoryIdentity.workspace_id == workspace_id,
                    ProviderInstallation.workspace_id == workspace_id,
                    ProviderInstallation.provider == "bitbucket",
                    ProviderInstallation.deleted_at.is_(None),
                    BitbucketConnection.workspace_id == workspace_id,
                    BitbucketConnection.revoked_at.is_(None),
                    Repository.installation_available.is_(True),
                )
                .order_by(Repository.full_name, Repository.id)
            )
        ).all()
        return tuple(RepositoryBinding(*row) for row in rows)

    async def create_connection(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        bitbucket_workspace_uuid: UUID,
        token: EncryptedCredentialLike,
    ) -> None:
        workspace = await self._session.scalar(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise BitbucketManagementError("bitbucket_workspace_not_found")
        external_id = await self._session.scalar(
            select(bitbucket_installation_external_id_seq.next_value())
        )
        if external_id is None:
            raise RuntimeError("bitbucket_external_id_unavailable")
        self._session.add(
            ProviderInstallation(
                id=installation_id,
                workspace_id=workspace_id,
                provider="bitbucket",
                external_id=external_id,
                provider_metadata={},
            )
        )
        await self._session.flush()
        self._session.add(
            BitbucketInstallationIdentity(
                provider_installation_id=installation_id,
                workspace_id=workspace_id,
                bitbucket_workspace_uuid=bitbucket_workspace_uuid,
            )
        )
        await self._session.flush()
        self._session.add(
            BitbucketConnection(
                provider_installation_id=installation_id,
                workspace_id=workspace_id,
                credential_generation=1,
                token_ciphertext=token.ciphertext,
                token_key_id=token.key_id,
                intake_enabled=False,
            )
        )
        await self._session.flush()

    async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None:
        return await self._rules.get_active_rule_version(repository_id)

    async def get_or_create_initial_rule_version(
        self, repository_id: UUID, version: int, rules: list[dict[str, object]]
    ) -> PersistedRuleVersion:
        return await self._rules.get_or_create_initial_rule_version(repository_id, version, rules)

    async def request_rules_refresh(self, repository_id: UUID) -> None:
        await request_rules_refresh(self._session, repository_id)

    async def bind_repository(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        repository_uuid: UUID,
        full_name: str,
        default_branch: str,
        web_url: str,
    ) -> tuple[UUID, bool]:
        parent, connection = await self._active_connection(workspace_id, installation_id)
        if parent is None or connection is None:
            raise BitbucketManagementError("bitbucket_connection_inactive")
        identity = await self._session.scalar(
            select(BitbucketRepositoryIdentity)
            .where(
                BitbucketRepositoryIdentity.workspace_id == workspace_id,
                BitbucketRepositoryIdentity.repository_uuid == repository_uuid,
            )
            .with_for_update()
        )
        if identity is not None:
            if identity.provider_installation_id != installation_id:
                raise BitbucketManagementError("repository_uuid_conflict")
            repository = await self._session.scalar(
                select(Repository).where(Repository.id == identity.repository_id).with_for_update()
            )
            if repository is None:
                raise RuntimeError("bitbucket_repository_identity_broken")
            reactivated = not repository.installation_available
            if reactivated:
                binding = await self._session.scalar(
                    select(BitbucketRepositoryWebhookSecret)
                    .where(BitbucketRepositoryWebhookSecret.repository_id == repository.id)
                    .with_for_update()
                )
                if binding is None:
                    raise RuntimeError("bitbucket_repository_binding_missing")
                binding.binding_generation += 1
            repository.installation_available = True
            repository.full_name = full_name
            repository.default_branch = default_branch
            repository.web_url = web_url
            await self._session.flush()
            return repository.id, reactivated
        external_id = await self._session.scalar(
            select(bitbucket_repository_external_id_seq.next_value())
        )
        if external_id is None:
            raise RuntimeError("bitbucket_repository_external_id_unavailable")
        repository_id = uuid4()
        self._session.add(
            Repository(
                id=repository_id,
                provider_installation_id=installation_id,
                external_id=external_id,
                full_name=full_name,
                default_branch=default_branch,
                web_url=web_url,
                installation_available=True,
                wait_for_ci=WaitForCi.NEVER,
            )
        )
        await self._session.flush()
        self._session.add(
            BitbucketRepositoryIdentity(
                repository_id=repository_id,
                provider_installation_id=installation_id,
                workspace_id=workspace_id,
                repository_external_id=external_id,
                repository_uuid=repository_uuid,
            )
        )
        await self._session.flush()
        self._session.add(
            BitbucketRepositoryWebhookSecret(repository_id=repository_id, binding_generation=1)
        )
        await self._session.flush()
        return repository_id, True

    async def set_repository_webhook_secret(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        repository_id: UUID,
        secret: EncryptedCredentialLike,
    ) -> int:
        parent, connection = await self._active_connection(workspace_id, installation_id)
        if parent is None or connection is None:
            raise BitbucketManagementError("bitbucket_connection_inactive")
        identity = await self._session.scalar(
            select(BitbucketRepositoryIdentity.repository_id).where(
                BitbucketRepositoryIdentity.repository_id == repository_id,
                BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                BitbucketRepositoryIdentity.workspace_id == workspace_id,
            )
        )
        if identity is None:
            raise BitbucketManagementError("bitbucket_repository_not_found")
        repository = await self._session.scalar(
            select(Repository).where(Repository.id == repository_id).with_for_update()
        )
        if repository is None or not repository.installation_available:
            raise BitbucketManagementError("bitbucket_repository_inactive")
        credential = await self._session.scalar(
            select(BitbucketRepositoryWebhookSecret)
            .where(BitbucketRepositoryWebhookSecret.repository_id == repository_id)
            .with_for_update()
        )
        if credential is None:
            self._session.add(
                BitbucketRepositoryWebhookSecret(
                    repository_id=repository_id,
                    generation=1,
                    secret_ciphertext=secret.ciphertext,
                    secret_key_id=secret.key_id,
                )
            )
            generation = 1
        else:
            credential.generation += 1
            credential.secret_ciphertext = secret.ciphertext
            credential.secret_key_id = secret.key_id
            credential.revoked_at = None
            credential.confirmed_at = None
            generation = credential.generation
        await self._session.flush()
        return generation

    async def disconnect_repository(
        self, *, workspace_id: UUID, installation_id: UUID, repository_id: UUID
    ) -> bool:
        parent, connection = await self._active_connection(workspace_id, installation_id)
        if parent is None or connection is None:
            return False
        identity = await self._session.scalar(
            select(BitbucketRepositoryIdentity)
            .where(
                BitbucketRepositoryIdentity.workspace_id == workspace_id,
                BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                BitbucketRepositoryIdentity.repository_id == repository_id,
            )
            .with_for_update()
        )
        if identity is None:
            return False
        repository = await self._session.scalar(
            select(Repository).where(Repository.id == repository_id).with_for_update()
        )
        if repository is None or not repository.installation_available:
            return False
        repository.installation_available = False
        webhook = await self._session.scalar(
            select(BitbucketRepositoryWebhookSecret)
            .where(BitbucketRepositoryWebhookSecret.repository_id == repository_id)
            .with_for_update()
        )
        if webhook is None:
            raise RuntimeError("bitbucket_repository_binding_missing")
        webhook.secret_ciphertext = None
        webhook.secret_key_id = None
        webhook.revoked_at = datetime.now(UTC)
        webhook.confirmed_at = None
        webhook.generation += 1
        webhook.binding_generation += 1
        await self._session.flush()
        remaining = await self._session.scalar(
            select(func.count())
            .select_from(Repository)
            .where(
                Repository.provider_installation_id == installation_id,
                Repository.installation_available.is_(True),
            )
        )
        if remaining == 0:
            now = datetime.now(UTC)
            parent.deleted_at = now
            connection.revoked_at = now
            connection.credential_generation += 1
            connection.token_ciphertext = None
            connection.token_key_id = None
            connection.intake_enabled = False
        await self._session.flush()
        return True

    async def rotate_token(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        token: EncryptedCredentialLike,
    ) -> int:
        parent, connection = await self._active_connection(workspace_id, installation_id)
        if parent is None or connection is None:
            raise BitbucketManagementError("bitbucket_connection_inactive")
        connection.token_ciphertext = token.ciphertext
        connection.token_key_id = token.key_id
        connection.credential_generation += 1
        await self._session.flush()
        return connection.credential_generation

    async def refresh_repository_metadata(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        repository_id: UUID,
        full_name: str,
        default_branch: str,
        web_url: str,
    ) -> None:
        identity = await self._session.scalar(
            select(BitbucketRepositoryIdentity.repository_id).where(
                BitbucketRepositoryIdentity.workspace_id == workspace_id,
                BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                BitbucketRepositoryIdentity.repository_id == repository_id,
            )
        )
        if identity is None:
            raise BitbucketManagementError("bitbucket_repository_not_found")
        repository = await self._session.scalar(
            select(Repository).where(Repository.id == repository_id).with_for_update()
        )
        if repository is None or not repository.installation_available:
            raise BitbucketManagementError("bitbucket_repository_inactive")
        repository.full_name = full_name
        repository.default_branch = default_branch
        repository.web_url = web_url
        await self._session.flush()

    async def revoke_connection(self, *, workspace_id: UUID, installation_id: UUID) -> bool:
        parent, connection = await self._active_connection(workspace_id, installation_id)
        if parent is None or connection is None:
            return False
        now = datetime.now(UTC)
        parent.deleted_at = now
        connection.revoked_at = now
        connection.credential_generation += 1
        connection.token_ciphertext = None
        connection.token_key_id = None
        connection.intake_enabled = False
        repositories = await self._session.scalars(
            select(Repository)
            .where(Repository.provider_installation_id == installation_id)
            .order_by(Repository.id)
            .with_for_update()
        )
        for repository in repositories:
            repository.installation_available = False
        webhook_credentials = await self._session.scalars(
            select(BitbucketRepositoryWebhookSecret)
            .join(
                BitbucketRepositoryIdentity,
                BitbucketRepositoryIdentity.repository_id
                == BitbucketRepositoryWebhookSecret.repository_id,
            )
            .where(BitbucketRepositoryIdentity.provider_installation_id == installation_id)
            .order_by(BitbucketRepositoryWebhookSecret.repository_id)
            .with_for_update()
        )
        for webhook in webhook_credentials:
            webhook.secret_ciphertext = None
            webhook.secret_key_id = None
            webhook.confirmed_at = None
            webhook.revoked_at = now
            webhook.generation += 1
            webhook.binding_generation += 1
        await self._session.flush()
        return True

    async def _active_connection(
        self, workspace_id: UUID, installation_id: UUID
    ) -> tuple[ProviderInstallation | None, BitbucketConnection | None]:
        parent = await self._session.scalar(
            select(ProviderInstallation)
            .where(
                ProviderInstallation.id == installation_id,
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.provider == "bitbucket",
            )
            .with_for_update()
        )
        if parent is None or parent.deleted_at is not None:
            return None, None
        connection = await self._session.scalar(
            select(BitbucketConnection)
            .where(
                BitbucketConnection.provider_installation_id == installation_id,
                BitbucketConnection.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if connection is None or connection.revoked_at is not None:
            return None, None
        return parent, connection


class SqlAlchemyBitbucketManagementUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def connections(self) -> SqlAlchemyBitbucketConnectionStore:
        return SqlAlchemyBitbucketConnectionStore(self.session)
