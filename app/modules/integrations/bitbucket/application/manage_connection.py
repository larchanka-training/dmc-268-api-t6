"""Portal-scoped Bitbucket connection lifecycle with provider reads outside transactions."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from app.common.application.unit_of_work import UnitOfWork
from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.application.onboard_repository import (
    DefaultRuleSet,
    OnboardRepository,
    RepositoryRuleVersionStore,
)


class BitbucketManagementError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EncryptedCredentialLike(Protocol):
    @property
    def key_id(self) -> str: ...

    @property
    def ciphertext(self) -> bytes: ...


@dataclass(frozen=True)
class InstallationSnapshot:
    installation_id: UUID
    generation: int
    workspace_uuid: UUID
    token_key_id: str = field(repr=False)
    token_ciphertext: bytes = field(repr=False)


@dataclass(frozen=True)
class RepositoryBinding:
    installation_id: UUID
    repository_id: UUID
    repository_uuid: UUID
    full_name: str
    web_url: str
    credential_generation: int
    binding_generation: int
    confirmed_at: datetime | None

    @property
    def workspace_slug(self) -> str:
        return self.full_name.partition("/")[0]

    @property
    def repository_slug(self) -> str:
        return self.full_name.partition("/")[2]

    @property
    def status(self) -> Literal["awaiting_webhook", "active"]:
        return "active" if self.confirmed_at is not None else "awaiting_webhook"


@dataclass(frozen=True)
class ConnectResult:
    installation_id: UUID
    repository_id: UUID
    repository_uuid: UUID
    workspace_slug: str
    repository_slug: str
    credential_generation: int
    webhook_secret: str | None = field(repr=False)
    confirmed: bool = False

    @property
    def status(self) -> Literal["awaiting_webhook", "active"]:
        return "active" if self.confirmed else "awaiting_webhook"


@dataclass(frozen=True)
class WebhookSecretResult:
    url: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class CredentialRotateResult:
    installation_id: UUID
    generation: int


class RemoteRepository(Protocol):
    @property
    def uuid(self) -> UUID: ...

    @property
    def workspace_uuid(self) -> UUID: ...

    @property
    def workspace_slug(self) -> str: ...

    @property
    def slug(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def full_name(self) -> str: ...

    @property
    def html_url(self) -> str: ...

    @property
    def default_branch(self) -> str: ...

    @property
    def language(self) -> str | None: ...

    @property
    def is_private(self) -> bool: ...


class RemoteRepositoryPage(Protocol):
    @property
    def repositories(self) -> tuple[RemoteRepository, ...]: ...

    @property
    def next_cursor(self) -> str | None: ...


class BitbucketRepositoryProvider(Protocol):
    async def fetch_repository(
        self, *, workspace_slug: str, repository_slug: str, token: str
    ) -> RemoteRepository: ...

    async def discover(
        self, *, workspace_slug: str, token: str, cursor: str | None = None
    ) -> RemoteRepositoryPage: ...

    async def fetch_repository_by_uuid(
        self, *, workspace_uuid: UUID, repository_uuid: UUID, token: str
    ) -> RemoteRepository: ...

    async def probe_pull_request_read(
        self, *, workspace_uuid: UUID, repository_uuid: UUID, token: str
    ) -> None: ...


class BitbucketCredentialCodec(Protocol):
    def encrypt_token(
        self, token: str, *, installation_id: UUID, workspace_id: UUID
    ) -> EncryptedCredentialLike: ...

    def decrypt_token(self, snapshot: InstallationSnapshot, *, workspace_id: UUID) -> str: ...

    def encrypt_webhook_secret(
        self,
        secret: str,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        repository_id: UUID,
    ) -> EncryptedCredentialLike: ...


class BitbucketManagementStore(RepositoryRuleVersionStore, Protocol):
    async def is_member(self, *, user_id: int, workspace_id: UUID, lock: bool = False) -> bool: ...

    async def installation_for_remote_workspace(
        self, *, workspace_id: UUID, bitbucket_workspace_uuid: UUID
    ) -> InstallationSnapshot | None: ...

    async def installation_by_id(
        self, *, workspace_id: UUID, installation_id: UUID
    ) -> InstallationSnapshot | None: ...

    async def revoked_installation_for_remote_workspace(
        self, *, workspace_id: UUID, bitbucket_workspace_uuid: UUID
    ) -> UUID | None: ...

    async def reopen_connection(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        token: EncryptedCredentialLike,
    ) -> int: ...

    async def create_connection(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        bitbucket_workspace_uuid: UUID,
        token: EncryptedCredentialLike,
    ) -> None: ...

    async def bind_repository(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        repository_uuid: UUID,
        full_name: str,
        default_branch: str,
        web_url: str,
    ) -> tuple[UUID, bool]: ...

    async def request_rules_refresh(self, repository_id: UUID) -> None: ...

    async def set_repository_webhook_secret(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        repository_id: UUID,
        secret: EncryptedCredentialLike,
    ) -> int: ...

    async def list_bindings(self, *, workspace_id: UUID) -> tuple[RepositoryBinding, ...]: ...

    async def rotate_token(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        token: EncryptedCredentialLike,
    ) -> int: ...

    async def refresh_repository_metadata(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        repository_id: UUID,
        full_name: str,
        default_branch: str,
        web_url: str,
    ) -> None: ...

    async def disconnect_repository(
        self, *, workspace_id: UUID, installation_id: UUID, repository_id: UUID
    ) -> bool: ...


class BitbucketManagementUnitOfWork(UnitOfWork, Protocol):
    @property
    def connections(self) -> BitbucketManagementStore: ...


class ManageBitbucketConnection:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], BitbucketManagementUnitOfWork],
        cipher: BitbucketCredentialCodec,
        provider: BitbucketRepositoryProvider,
        rule_sets: Mapping[str, DefaultRuleSet],
        webhook_base_url: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._cipher = cipher
        self._provider = provider
        self._rule_sets = rule_sets
        self._webhook_base_url = webhook_base_url.rstrip("/")

    async def _require_member(
        self, store: BitbucketManagementStore, scope: AuthScope, workspace_id: UUID, *, lock: bool
    ) -> None:
        if workspace_id not in scope.workspace_ids or not await store.is_member(
            user_id=scope.user_id, workspace_id=workspace_id, lock=lock
        ):
            raise BitbucketManagementError("workspace_not_found")

    async def connect(
        self,
        *,
        scope: AuthScope,
        workspace_id: UUID,
        workspace_slug: str,
        repository_slug: str,
        repository_uuid: UUID,
        token: str,
    ) -> ConnectResult:
        async with self._uow_factory() as uow:
            await self._require_member(uow.connections, scope, workspace_id, lock=False)
        remote = await self._provider.fetch_repository(
            workspace_slug=workspace_slug, repository_slug=repository_slug, token=token
        )
        if remote.uuid != repository_uuid:
            raise BitbucketManagementError("repository_uuid_mismatch")
        await self._provider.probe_pull_request_read(
            workspace_uuid=remote.workspace_uuid, repository_uuid=remote.uuid, token=token
        )
        generated_secret: str | None = None
        async with self._uow_factory() as uow:
            store = uow.connections
            await self._require_member(store, scope, workspace_id, lock=True)
            existing = await store.installation_for_remote_workspace(
                workspace_id=workspace_id, bitbucket_workspace_uuid=remote.workspace_uuid
            )
            if existing is None:
                revoked_id = await store.revoked_installation_for_remote_workspace(
                    workspace_id=workspace_id,
                    bitbucket_workspace_uuid=remote.workspace_uuid,
                )
                installation_id = revoked_id or uuid4()
                encrypted_token = self._cipher.encrypt_token(
                    token, installation_id=installation_id, workspace_id=workspace_id
                )
                if revoked_id is None:
                    await store.create_connection(
                        workspace_id=workspace_id,
                        installation_id=installation_id,
                        bitbucket_workspace_uuid=remote.workspace_uuid,
                        token=encrypted_token,
                    )
                else:
                    await store.reopen_connection(
                        workspace_id=workspace_id,
                        installation_id=installation_id,
                        token=encrypted_token,
                    )
            else:
                installation_id = existing.installation_id
                current_token = self._cipher.decrypt_token(existing, workspace_id=workspace_id)
                if not hmac.compare_digest(token, current_token):
                    raise BitbucketManagementError("credential_rotation_required")
                generation = existing.generation
            try:
                repository_id, newly_active = await store.bind_repository(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    repository_uuid=remote.uuid,
                    full_name=remote.full_name,
                    default_branch=remote.default_branch,
                    web_url=remote.html_url,
                )
            except ValueError as error:
                if str(error) == "bitbucket_connection_inactive":
                    raise BitbucketManagementError("connection_changed") from None
                raise
            language_sizes = {remote.language: 1} if remote.language else {}
            await OnboardRepository(store, self._rule_sets).execute(repository_id, language_sizes)
            if newly_active:
                await store.request_rules_refresh(repository_id)
                generated_secret = secrets.token_urlsafe(36)
                await store.set_repository_webhook_secret(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    repository_id=repository_id,
                    secret=self._cipher.encrypt_webhook_secret(
                        generated_secret,
                        installation_id=installation_id,
                        workspace_id=workspace_id,
                        repository_id=repository_id,
                    ),
                )
            confirmed = False
            if not newly_active:
                confirmed = any(
                    binding.repository_id == repository_id and binding.confirmed_at is not None
                    for binding in await store.list_bindings(workspace_id=workspace_id)
                )
            current = await store.installation_for_remote_workspace(
                workspace_id=workspace_id, bitbucket_workspace_uuid=remote.workspace_uuid
            )
            if current is None:
                raise BitbucketManagementError("connection_changed")
            generation = current.generation
            await uow.commit()
        return ConnectResult(
            installation_id,
            repository_id,
            remote.uuid,
            remote.workspace_slug,
            remote.slug,
            generation,
            generated_secret,
            confirmed,
        )

    async def discover(
        self,
        *,
        scope: AuthScope,
        workspace_id: UUID,
        workspace_slug: str,
        token: str,
        cursor: str | None,
    ) -> RemoteRepositoryPage:
        async with self._uow_factory() as uow:
            await self._require_member(uow.connections, scope, workspace_id, lock=False)
        return await self._provider.discover(
            workspace_slug=workspace_slug, token=token, cursor=cursor
        )

    async def list_connections(
        self, *, scope: AuthScope, workspace_id: UUID
    ) -> tuple[RepositoryBinding, ...]:
        async with self._uow_factory() as uow:
            await self._require_member(uow.connections, scope, workspace_id, lock=False)
            return await uow.connections.list_bindings(workspace_id=workspace_id)

    async def disconnect(
        self,
        *,
        scope: AuthScope,
        workspace_id: UUID,
        installation_id: UUID,
        repository_id: UUID,
    ) -> None:
        async with self._uow_factory() as uow:
            await self._require_member(uow.connections, scope, workspace_id, lock=True)
            await uow.connections.disconnect_repository(
                workspace_id=workspace_id,
                installation_id=installation_id,
                repository_id=repository_id,
            )
            await uow.commit()

    async def rotate_webhook_secret(
        self,
        *,
        scope: AuthScope,
        workspace_id: UUID,
        installation_id: UUID,
        repository_id: UUID,
    ) -> WebhookSecretResult:
        secret = secrets.token_urlsafe(36)
        async with self._uow_factory() as uow:
            await self._require_member(uow.connections, scope, workspace_id, lock=True)
            encrypted = self._cipher.encrypt_webhook_secret(
                secret,
                installation_id=installation_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
            )
            try:
                await uow.connections.set_repository_webhook_secret(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    repository_id=repository_id,
                    secret=encrypted,
                )
            except ValueError:
                raise BitbucketManagementError("repository_not_found") from None
            await uow.commit()
        return WebhookSecretResult(self.webhook_url(installation_id, repository_id), secret)

    async def rotate_token(
        self,
        *,
        scope: AuthScope,
        workspace_id: UUID,
        installation_id: UUID,
        token: str,
    ) -> CredentialRotateResult:
        async with self._uow_factory() as uow:
            await self._require_member(uow.connections, scope, workspace_id, lock=False)
            prior = await uow.connections.installation_by_id(
                workspace_id=workspace_id, installation_id=installation_id
            )
            bindings = tuple(
                binding
                for binding in await uow.connections.list_bindings(workspace_id=workspace_id)
                if binding.installation_id == installation_id
            )
        if prior is None or not bindings:
            raise BitbucketManagementError("connection_not_found")
        refreshed: list[tuple[RepositoryBinding, RemoteRepository]] = []
        for binding in bindings:
            remote = await self._provider.fetch_repository_by_uuid(
                workspace_uuid=prior.workspace_uuid,
                repository_uuid=binding.repository_uuid,
                token=token,
            )
            if remote.workspace_uuid != prior.workspace_uuid:
                raise BitbucketManagementError("repository_uuid_conflict")
            await self._provider.probe_pull_request_read(
                workspace_uuid=prior.workspace_uuid,
                repository_uuid=binding.repository_uuid,
                token=token,
            )
            refreshed.append((binding, remote))
        async with self._uow_factory() as uow:
            await self._require_member(uow.connections, scope, workspace_id, lock=True)
            current = await uow.connections.installation_by_id(
                workspace_id=workspace_id, installation_id=installation_id
            )
            current_bindings = tuple(
                binding
                for binding in await uow.connections.list_bindings(workspace_id=workspace_id)
                if binding.installation_id == installation_id
            )
            if (
                current is None
                or current.generation != prior.generation
                or current.workspace_uuid != prior.workspace_uuid
                or tuple(
                    (item.repository_id, item.repository_uuid, item.binding_generation)
                    for item in current_bindings
                )
                != tuple(
                    (item.repository_id, item.repository_uuid, item.binding_generation)
                    for item in bindings
                )
            ):
                raise BitbucketManagementError("connection_changed")
            try:
                generation = await uow.connections.rotate_token(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    token=self._cipher.encrypt_token(
                        token, installation_id=installation_id, workspace_id=workspace_id
                    ),
                )
            except ValueError as error:
                if str(error) == "bitbucket_connection_inactive":
                    raise BitbucketManagementError("connection_changed") from None
                raise
            for binding, remote in refreshed:
                await uow.connections.refresh_repository_metadata(
                    workspace_id=workspace_id,
                    installation_id=installation_id,
                    repository_id=binding.repository_id,
                    full_name=remote.full_name,
                    default_branch=remote.default_branch,
                    web_url=remote.html_url,
                )
            await uow.commit()
        return CredentialRotateResult(installation_id, generation)

    def webhook_url(self, installation_id: UUID, repository_id: UUID) -> str:
        return f"{self._webhook_base_url}/webhooks/bitbucket/{installation_id}/{repository_id}"
