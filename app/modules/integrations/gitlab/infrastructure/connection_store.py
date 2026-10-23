"""Flush-only GitLab management persistence and concrete credential adaptation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import RunState, WaitForCi
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.gitlab.application.manage_connection import (
    ActivationReadiness,
    ActivationTarget,
    BindingInspection,
    ConnectionInspection,
    ConnectionResult,
    ConnectionSnapshot,
    CredentialEnvelope,
    GitLabManagementError,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    CredentialCipher,
    CredentialContext,
    EncryptedCredential,
    digest_webhook_secret,
)
from app.modules.integrations.gitlab.infrastructure.models import (
    GitLabConnection,
    GitLabProjectBinding,
    gitlab_installation_external_id_seq,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import PersistedRuleVersion
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RuleVersion,
)
from app.modules.repositories.infrastructure.rule_version_repository import (
    SqlAlchemyRepositoryRuleVersionStore,
)
from app.modules.reviews.infrastructure.models import PromptVersion, Run
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from app.modules.workspaces.infrastructure.models import Workspace


class CredentialCipherAdapter:
    """Translate application values to the concrete codec's runtime checked types."""

    def __init__(self, cipher: CredentialCipher) -> None:
        self._cipher = cipher

    def encrypt(
        self, token: str, *, installation_id: UUID, workspace_id: UUID, instance_origin: str
    ) -> CredentialEnvelope:
        try:
            encrypted = self._cipher.encrypt(
                token, context=CredentialContext(installation_id, workspace_id, instance_origin)
            )
            return CredentialEnvelope(encrypted.key_id, encrypted.token_ciphertext)
        except ValueError:
            raise GitLabManagementError("credential_operation_failed") from None

    def decrypt(
        self,
        credential: CredentialEnvelope,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
    ) -> str:
        try:
            return self._cipher.decrypt(
                EncryptedCredential(credential.key_id, credential.ciphertext),
                context=CredentialContext(installation_id, workspace_id, instance_origin),
            )
        except ValueError:
            raise GitLabManagementError("credential_operation_failed") from None

    def webhook_digest(self, secret: str) -> str:
        try:
            return digest_webhook_secret(secret)
        except ValueError:
            raise GitLabManagementError("invalid_webhook_secret") from None


class SqlAlchemyGitLabConnectionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._rules = SqlAlchemyRepositoryRuleVersionStore(session)

    async def workspace_exists(self, workspace_id: UUID, *, lock: bool = False) -> bool:
        statement = select(Workspace.id).where(Workspace.id == workspace_id)
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement) is not None

    # Locked calls follow the application workspace fence: installation, connection,
    # own bindings and repositories. No provider call can occur in this UOW.
    async def connection_snapshot(
        self, installation_id: UUID, *, lock: bool = False
    ) -> ConnectionSnapshot | None:
        parent_statement = select(ProviderInstallation).where(
            ProviderInstallation.id == installation_id, ProviderInstallation.provider == "gitlab"
        )
        if lock:
            parent_statement = parent_statement.with_for_update()
        parent = await self._session.scalar(parent_statement)
        if parent is None:
            return None
        statement = select(GitLabConnection).where(
            GitLabConnection.provider_installation_id == installation_id
        )
        if lock:
            statement = statement.with_for_update()
        connection = await self._session.scalar(statement)
        if connection is None:
            return None
        bindings_statement = (
            select(GitLabProjectBinding)
            .where(GitLabProjectBinding.provider_installation_id == installation_id)
            .order_by(GitLabProjectBinding.project_external_id)
        )
        if lock:
            bindings_statement = bindings_statement.with_for_update()
        bindings = tuple(await self._session.scalars(bindings_statement))
        if lock and bindings:
            await self._session.execute(
                select(Repository.id)
                .where(Repository.id.in_([item.repository_id for item in bindings]))
                .order_by(Repository.id)
                .with_for_update()
            )
        credential = None
        if connection.token_ciphertext is not None and connection.credential_key_id is not None:
            credential = CredentialEnvelope(
                connection.credential_key_id, connection.token_ciphertext
            )
        return ConnectionSnapshot(
            installation_id,
            parent.workspace_id,
            connection.instance_origin,
            connection.credential_generation,
            credential,
            revoked=parent.deleted_at is not None or connection.revoked_at is not None,
            intake_enabled=connection.intake_enabled,
            bindings=tuple((item.project_external_id, item.repository_id) for item in bindings),
        )

    async def create_connection(self, snapshot: ConnectionSnapshot, webhook_digest: str) -> None:
        assert snapshot.credential is not None
        external_id = await self._session.scalar(
            select(gitlab_installation_external_id_seq.next_value())
        )
        self._session.add(
            ProviderInstallation(
                id=snapshot.installation_id,
                workspace_id=snapshot.workspace_id,
                provider="gitlab",
                external_id=external_id,
                provider_metadata={},
            )
        )
        try:
            await self._session.flush()
            self._session.add(
                GitLabConnection(
                    provider_installation_id=snapshot.installation_id,
                    workspace_id=snapshot.workspace_id,
                    instance_origin=snapshot.instance_origin,
                    credential_generation=1,
                    token_ciphertext=snapshot.credential.ciphertext,
                    credential_key_id=snapshot.credential.key_id,
                    webhook_secret_digest=webhook_digest,
                    intake_enabled=False,
                )
            )
            await self._session.flush()
        except IntegrityError:
            raise GitLabManagementError("installation_identity_conflict") from None

    async def bind_project(
        self, snapshot: ConnectionSnapshot, project: RepositorySnapshot
    ) -> tuple[UUID, bool]:
        # Do not lock another installation's reservation: opposite cross-claims
        # could invert their owner locks. The unique insert below serializes new
        # claims, and an observed foreign owner is a safe conflict/retry.
        reservation = await self._session.scalar(
            select(GitLabProjectBinding).where(
                GitLabProjectBinding.instance_origin == snapshot.instance_origin,
                GitLabProjectBinding.project_external_id == project.external_id,
            )
        )
        if (
            reservation is not None
            and reservation.provider_installation_id != snapshot.installation_id
        ):
            raise GitLabManagementError("project_reserved")
        statement = insert(Repository).values(
            provider_installation_id=snapshot.installation_id,
            external_id=project.external_id,
            full_name=project.full_name,
            default_branch=project.default_branch,
            web_url=project.web_url,
            wait_for_ci=WaitForCi.NEVER,
        )
        upsert = statement.on_conflict_do_update(
            index_elements=[Repository.provider_installation_id, Repository.external_id],
            set_={
                "full_name": project.full_name,
                "default_branch": project.default_branch,
                "web_url": project.web_url,
                "installation_available": True,
            },
        ).returning(Repository.id)
        repository_id = await self._session.scalar(upsert)
        if repository_id is None:
            raise GitLabManagementError("binding_failed")
        inserted = await self._session.scalar(
            insert(GitLabProjectBinding)
            .values(
                repository_id=repository_id,
                provider_installation_id=snapshot.installation_id,
                instance_origin=snapshot.instance_origin,
                project_external_id=project.external_id,
            )
            .on_conflict_do_nothing()
            .returning(GitLabProjectBinding.repository_id)
        )
        if inserted is None:
            owner = await self._session.scalar(
                select(GitLabProjectBinding.provider_installation_id).where(
                    GitLabProjectBinding.instance_origin == snapshot.instance_origin,
                    GitLabProjectBinding.project_external_id == project.external_id,
                )
            )
            if owner != snapshot.installation_id:
                raise GitLabManagementError("project_reserved")
        await self._session.flush()
        return repository_id, inserted is not None

    async def activation_readiness(self, snapshot: ConnectionSnapshot) -> ActivationReadiness:
        # Current locked rows, never the informational inspection or runtime grant.
        parent = await self._session.get(ProviderInstallation, snapshot.installation_id)
        connection = await self._session.get(GitLabConnection, snapshot.installation_id)
        bindings = tuple(
            await self._session.scalars(
                select(GitLabProjectBinding)
                .where(GitLabProjectBinding.provider_installation_id == snapshot.installation_id)
                .order_by(GitLabProjectBinding.project_external_id)
            )
        )
        repositories = {
            item.id: item
            for item in await self._session.scalars(
                select(Repository).where(Repository.id.in_([repo for _, repo in snapshot.bindings]))
            )
        }
        identity_valid = (
            parent is not None
            and parent.provider == "gitlab"
            and parent.workspace_id == snapshot.workspace_id
            and parent.deleted_at is None
            and connection is not None
            and connection.workspace_id == snapshot.workspace_id
            and connection.instance_origin == snapshot.instance_origin
            and tuple((item.project_external_id, item.repository_id) for item in bindings)
            == snapshot.bindings
            and all(
                binding.instance_origin == snapshot.instance_origin
                and (repository := repositories.get(binding.repository_id)) is not None
                and repository.provider_installation_id == snapshot.installation_id
                and repository.external_id == binding.project_external_id
                for binding in bindings
            )
        )
        # Existing repo locks precede deterministic active configuration row locks.
        rules = tuple(
            await self._session.scalars(
                select(RuleVersion)
                .where(RuleVersion.repository_id.in_(repositories), RuleVersion.is_active.is_(True))
                .order_by(RuleVersion.id)
                .with_for_update()
            )
        )
        prompts = tuple(
            await self._session.scalars(
                select(PromptVersion.id)
                .where(PromptVersion.key == "review.system", PromptVersion.is_active.is_(True))
                .order_by(PromptVersion.id)
                .with_for_update()
            )
        )
        rule_repositories = {rule.repository_id for rule in rules}
        return ActivationReadiness(
            identity_valid,
            bool(prompts),
            tuple(
                ActivationTarget(
                    repository_id,
                    project_id,
                    repository.enabled,
                    repository.installation_available,
                    repository.wait_for_ci == WaitForCi.NEVER,
                    repository_id in rule_repositories,
                )
                for project_id, repository_id in snapshot.bindings
                if (repository := repositories.get(repository_id)) is not None
            ),
        )

    async def enable_intake(self, snapshot: ConnectionSnapshot) -> ConnectionSnapshot:
        # Reuse the caller's workspace/installation/connection/binding/repo locks.
        current = await self.connection_snapshot(snapshot.installation_id)
        if current != snapshot or snapshot.revoked or snapshot.credential is None:
            raise GitLabManagementError("connection_changed")
        connection = await self._session.get(GitLabConnection, snapshot.installation_id)
        assert connection is not None
        if not connection.intake_enabled:
            connection.intake_enabled = True
            await self._session.flush()
        return replace(snapshot, intake_enabled=True)

    async def rotate_credential(
        self, snapshot: ConnectionSnapshot, credential: CredentialEnvelope
    ) -> ConnectionSnapshot:
        connection = await self._session.get(GitLabConnection, snapshot.installation_id)
        assert connection is not None
        connection.token_ciphertext = credential.ciphertext
        connection.credential_key_id = credential.key_id
        connection.credential_generation += 1
        await self._retire_runs((snapshot,), reason="credential_rotated", now=datetime.now(UTC))
        return replace(snapshot, generation=connection.credential_generation, credential=credential)

    async def workspace_connections(self, workspace_id: UUID) -> tuple[ConnectionSnapshot, ...]:
        # Caller holds the workspace fence. Lock each complete level before the next:
        # all parents, all credentials, all bindings, all repositories, then runs.
        ids = tuple(
            await self._session.scalars(
                select(ProviderInstallation.id)
                .where(
                    ProviderInstallation.workspace_id == workspace_id,
                    ProviderInstallation.provider == "gitlab",
                )
                .order_by(ProviderInstallation.id)
                .with_for_update()
            )
        )
        if not ids:
            return ()
        await self._session.execute(
            select(GitLabConnection)
            .where(GitLabConnection.provider_installation_id.in_(ids))
            .order_by(GitLabConnection.provider_installation_id)
            .with_for_update()
        )
        await self._session.execute(
            select(GitLabProjectBinding)
            .where(GitLabProjectBinding.provider_installation_id.in_(ids))
            .order_by(
                GitLabProjectBinding.provider_installation_id,
                GitLabProjectBinding.project_external_id,
            )
            .with_for_update()
        )
        await self._session.execute(
            select(Repository.id)
            .where(Repository.provider_installation_id.in_(ids))
            .order_by(Repository.id)
            .with_for_update()
        )
        snapshots = []
        for installation_id in ids:
            snapshot = await self.connection_snapshot(installation_id)
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots)

    async def revoke_connections(
        self, snapshots: tuple[ConnectionSnapshot, ...]
    ) -> tuple[ConnectionSnapshot, ...]:
        if not snapshots:
            return ()
        ids = [snapshot.installation_id for snapshot in snapshots]
        repositories = tuple(
            await self._session.scalars(
                select(Repository)
                .where(Repository.provider_installation_id.in_(ids))
                .order_by(Repository.id)
                .with_for_update()
            )
        )
        now = datetime.now(UTC)
        updated = []
        for snapshot in snapshots:
            parent = await self._session.get(ProviderInstallation, snapshot.installation_id)
            connection = await self._session.get(GitLabConnection, snapshot.installation_id)
            assert parent is not None and connection is not None
            if connection.revoked_at is None:
                connection.revoked_at = now
                connection.credential_generation += 1
            if parent.deleted_at is None:
                parent.deleted_at = now
            connection.intake_enabled = False
            connection.token_ciphertext = None
            connection.credential_key_id = None
            updated.append(
                replace(
                    snapshot,
                    generation=connection.credential_generation,
                    credential=None,
                    revoked=True,
                    intake_enabled=False,
                    bindings=(),
                )
            )
        for repository in repositories:
            repository.installation_available = False
        await self._session.execute(
            delete(GitLabProjectBinding).where(
                GitLabProjectBinding.provider_installation_id.in_(ids)
            )
        )
        await self._retire_runs(snapshots, reason="installation_revoked", now=now)
        return tuple(updated)

    async def _retire_runs(
        self, snapshots: tuple[ConnectionSnapshot, ...], *, reason: str, now: datetime
    ) -> None:
        # The caller holds workspace, installation, connection and bound-repository locks.
        # Keep the immutable origin's tenant even after current repository ownership changes.
        workspaces = {snapshot.installation_id: snapshot.workspace_id for snapshot in snapshots}
        runs = tuple(
            await self._session.scalars(
                select(Run)
                .where(
                    Run.origin_provider_installation_id.in_(workspaces),
                    Run.state.in_((RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING)),
                )
                .order_by(Run.id)
                .with_for_update()
            )
        )
        # Unlike lifecycle.finish(None), retirement must also fence an unexpired lease.
        # Keep attempts and already-requested cancellation signals; release worker lease.
        for run in runs:
            run.state = RunState.CANCELLED
            run.cancel_requested = True
            run.error_code = reason
            run.finished_at = now
            run.lease_until = None
            run.worker_id = None
            if run.attempt >= 1 and run.cancellation_signal_requested_at is None:
                run.cancellation_signal_requested_at = now
        await self._session.flush()
        notifications = SqlAlchemyWebhookRunStore(self._session)
        for run in runs:
            origin = run.origin_provider_installation_id
            assert origin is not None
            await notifications.notify_run_updated(run.id, workspaces[origin], "cancelled")

    async def inspect_connection(self, installation_id: UUID) -> ConnectionInspection | None:
        snapshot = await self.connection_snapshot(installation_id)
        if snapshot is None:
            return None
        repositories = tuple(
            await self._session.scalars(
                select(Repository)
                .where(Repository.provider_installation_id == installation_id)
                .order_by(Repository.id)
            )
        )
        inspections = []
        for repository in repositories:
            inspections.append(
                BindingInspection(
                    repository.id,
                    repository.external_id,
                    repository.enabled,
                    repository.installation_available,
                    await self.get_active_rule_version(repository.id) is not None,
                )
            )
        return ConnectionInspection(
            ConnectionResult(
                snapshot.installation_id,
                snapshot.workspace_id,
                snapshot.instance_origin,
                snapshot.generation,
                snapshot.intake_enabled,
                snapshot.revoked,
            ),
            tuple(inspections),
            await self.prompt_ready(),
        )

    async def prompt_ready(self) -> bool:
        return (
            await self._session.scalar(
                select(PromptVersion.id)
                .where(PromptVersion.key == "review.system", PromptVersion.is_active.is_(True))
                .limit(1)
            )
            is not None
        )

    async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None:
        return await self._rules.get_active_rule_version(repository_id)

    async def get_or_create_initial_rule_version(
        self, repository_id: UUID, version: int, rules: list[dict[str, object]]
    ) -> PersistedRuleVersion:
        return await self._rules.get_or_create_initial_rule_version(repository_id, version, rules)


class SqlAlchemyGitLabManagementUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def connections(self) -> SqlAlchemyGitLabConnectionStore:
        return SqlAlchemyGitLabConnectionStore(self.session)
