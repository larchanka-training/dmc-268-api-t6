"""Trusted operator create/bind management; provider calls precede transactions."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import (
    DefaultRuleSet,
    OnboardRepository,
    RepositoryRuleVersionStore,
    RuleSetValidationError,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)


class GitLabManagementError(ValueError):
    """A stable, sanitized management outcome, safe to display to operators."""

    def __init__(self, code: str, *, remediation: str | None = None) -> None:
        self.code = code
        self.remediation = remediation
        super().__init__(f"{code}: {remediation}" if remediation else code)


@dataclass(frozen=True)
class GitLabOriginPolicy:
    """Explicit trusted origins; private hosts additionally require operator opt-in."""

    allowed_origins: frozenset[str]
    private_origins: frozenset[str] = frozenset()

    def validate(self, origin: str) -> str:
        try:
            parsed = urlsplit(origin)
            host = parsed.hostname
            if not isinstance(origin, str) or not origin.isascii() or len(origin) > 300 or not host:
                raise ValueError
            if len(host) > 253 or "%" in host or re.fullmatch(r"0x[0-9a-f]+", host):
                raise ValueError
            address: ipaddress.IPv4Address | ipaddress.IPv6Address | None
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None:
                canonical_host = f"[{address.compressed}]" if address.version == 6 else str(address)
                private = not address.is_global
            else:
                if (
                    any(
                        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                        for label in host.split(".")
                    )
                    or host.split(".")[-1].isdigit()
                ):
                    raise ValueError
                canonical_host = host
                private = "." not in host or host.endswith((".localhost", ".local", ".internal"))
            port = parsed.port
            suffix = f":{port}" if port is not None else ""
            if (
                parsed.scheme != "https"
                or port == 443
                or (port is not None and not 1 <= port <= 65535)
                or origin != f"https://{canonical_host}{suffix}"
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GitLabManagementError("invalid_origin") from None
        if origin not in self.allowed_origins:
            raise GitLabManagementError("origin_not_allowed")
        if private and origin not in self.private_origins:
            raise GitLabManagementError("private_origin_not_allowed")
        return origin


@dataclass(frozen=True)
class CredentialEnvelope:
    key_id: str = field(repr=False)
    ciphertext: bytes = field(repr=False)


@dataclass(frozen=True)
class ConnectionSnapshot:
    installation_id: UUID
    workspace_id: UUID
    instance_origin: str
    generation: int
    credential: CredentialEnvelope | None = field(repr=False)
    revoked: bool = False
    intake_enabled: bool = False
    bindings: tuple[tuple[int, UUID], ...] = ()


@dataclass(frozen=True)
class ConnectionResult:
    installation_id: UUID
    workspace_id: UUID
    instance_origin: str
    generation: int
    intake_enabled: bool
    revoked: bool


@dataclass(frozen=True)
class BindingResult:
    repository_id: UUID
    created: bool
    rules_created: bool


class GitLabCredentialCodec(Protocol):
    def encrypt(
        self, token: str, *, installation_id: UUID, workspace_id: UUID, instance_origin: str
    ) -> CredentialEnvelope: ...

    def decrypt(
        self,
        credential: CredentialEnvelope,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
    ) -> str: ...

    def webhook_digest(self, secret: str) -> str: ...


class GitLabProjectAccess(Protocol):
    async def authenticate(self, *, origin: str, token: str) -> None: ...

    async def fetch_project(
        self, *, origin: str, token: str, project_id: int
    ) -> RepositoryOnboardingInput: ...


class GitLabConnectionStore(RepositoryRuleVersionStore, Protocol):
    async def workspace_exists(self, workspace_id: UUID, *, lock: bool = False) -> bool: ...

    async def connection_snapshot(
        self, installation_id: UUID, *, lock: bool = False
    ) -> ConnectionSnapshot | None: ...

    async def create_connection(
        self, snapshot: ConnectionSnapshot, webhook_digest: str
    ) -> None: ...

    async def bind_project(
        self, snapshot: ConnectionSnapshot, project: RepositorySnapshot
    ) -> tuple[UUID, bool]: ...

    async def prompt_ready(self) -> bool: ...


class GitLabManagementUnitOfWork(UnitOfWork, Protocol):
    @property
    def connections(self) -> GitLabConnectionStore: ...


class ManageGitLabConnection:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], GitLabManagementUnitOfWork],
        codec: GitLabCredentialCodec,
        projects: GitLabProjectAccess,
        origin_policy: GitLabOriginPolicy,
        rule_sets: Mapping[str, DefaultRuleSet],
    ) -> None:
        self._uow_factory = uow_factory
        self._codec = codec
        self._projects = projects
        self._origins = origin_policy
        self._rule_sets = rule_sets

    async def create(
        self,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
        token: str,
        webhook_secret: str,
    ) -> ConnectionResult:
        origin = self._origins.validate(instance_origin)
        if not isinstance(installation_id, UUID) or not isinstance(workspace_id, UUID):
            raise GitLabManagementError("invalid_identity")
        async with self._uow_factory() as uow:
            if not await uow.connections.workspace_exists(workspace_id):
                raise GitLabManagementError("workspace_not_found")
            existing = await uow.connections.connection_snapshot(installation_id)
            if existing is not None:
                return _matching_create(existing, workspace_id, origin)
        credential = self._codec.encrypt(
            token,
            installation_id=installation_id,
            workspace_id=workspace_id,
            instance_origin=origin,
        )
        digest = self._codec.webhook_digest(webhook_secret)
        await self._projects.authenticate(origin=origin, token=token)
        snapshot = ConnectionSnapshot(installation_id, workspace_id, origin, 1, credential)
        async with self._uow_factory() as uow:
            if not await uow.connections.workspace_exists(workspace_id, lock=True):
                raise GitLabManagementError("workspace_not_found")
            existing = await uow.connections.connection_snapshot(installation_id, lock=True)
            if existing is not None:
                return _matching_create(existing, workspace_id, origin)
            await uow.connections.create_connection(snapshot, digest)
            await uow.commit()
        return _connection_result(snapshot)

    async def bind_project(self, *, installation_id: UUID, project_id: int) -> BindingResult:
        if (
            not isinstance(installation_id, UUID)
            or type(project_id) is not int
            or not 0 < project_id <= 9223372036854775807
        ):
            raise GitLabManagementError("invalid_identity")
        if set(self._rule_sets) != {"backend", "frontend"}:
            raise GitLabManagementError("rule_assets_missing")
        async with self._uow_factory() as uow:
            snapshot = await uow.connections.connection_snapshot(installation_id)
        if snapshot is None:
            raise GitLabManagementError("connection_not_found")
        _require_active(snapshot)
        origin = self._origins.validate(snapshot.instance_origin)
        assert snapshot.credential is not None
        token = self._codec.decrypt(
            snapshot.credential,
            installation_id=installation_id,
            workspace_id=snapshot.workspace_id,
            instance_origin=origin,
        )
        project = await self._projects.fetch_project(
            origin=origin, token=token, project_id=project_id
        )
        validate_project_snapshot(project.snapshot, origin=origin, project_id=project_id)
        if any(
            not isinstance(name, str) or not name or type(weight) is not int or weight <= 0
            for name, weight in project.languages.items()
        ):
            raise GitLabManagementError("invalid_languages")
        async with self._uow_factory() as uow:
            if not await uow.connections.workspace_exists(snapshot.workspace_id, lock=True):
                raise GitLabManagementError("workspace_not_found")
            locked = await uow.connections.connection_snapshot(installation_id, lock=True)
            if locked != snapshot:
                raise GitLabManagementError("connection_changed")
            _require_active(locked)
            repository_id, created = await uow.connections.bind_project(snapshot, project.snapshot)
            try:
                onboarded = await OnboardRepository(uow.connections, self._rule_sets).execute(
                    repository_id, project.languages
                )
            except RuleSetValidationError:
                raise GitLabManagementError("rule_assets_invalid") from None
            if not await uow.connections.prompt_ready():
                raise GitLabManagementError(
                    "prompt_not_seeded",
                    remediation="run uv run python -m app.bootstrap.seed_prompts",
                )
            await uow.commit()
        return BindingResult(repository_id, created, onboarded.created)


def _require_active(snapshot: ConnectionSnapshot) -> None:
    if snapshot.revoked or snapshot.credential is None:
        raise GitLabManagementError("connection_revoked")


def _connection_result(snapshot: ConnectionSnapshot) -> ConnectionResult:
    return ConnectionResult(
        snapshot.installation_id,
        snapshot.workspace_id,
        snapshot.instance_origin,
        snapshot.generation,
        snapshot.intake_enabled,
        snapshot.revoked,
    )


def _matching_create(
    snapshot: ConnectionSnapshot, workspace_id: UUID, origin: str
) -> ConnectionResult:
    if snapshot.workspace_id != workspace_id or snapshot.instance_origin != origin:
        raise GitLabManagementError("installation_identity_conflict")
    return _connection_result(snapshot)


def validate_project_snapshot(
    snapshot: RepositorySnapshot, *, origin: str, project_id: int
) -> None:
    """Enforce the same canonical identity at HTTP and application boundaries."""
    if type(snapshot.external_id) is not int or snapshot.external_id != project_id:
        raise GitLabManagementError("project_identity_mismatch")
    path = snapshot.full_name
    branch = snapshot.default_branch
    if (
        not isinstance(path, str)
        or not 1 <= len(path) <= 512
        or len(path.split("/")) < 2
        or any(
            re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) is None for part in path.split("/")
        )
        or not isinstance(branch, str)
        or not 1 <= len(branch) <= 255
        or any(ord(character) < 33 or ord(character) == 127 for character in branch)
        or snapshot.web_url != f"{origin}/{path}"
    ):
        raise GitLabManagementError("invalid_project")


@dataclass(frozen=True)
class BindingInspection:
    repository_id: UUID
    project_id: int
    enabled: bool
    available: bool
    rules_ready: bool


@dataclass(frozen=True)
class ConnectionInspection:
    connection: ConnectionResult
    repositories: tuple[BindingInspection, ...]
    prompt_ready: bool


@dataclass(frozen=True)
class ActivationTarget:
    repository_id: UUID
    project_id: int
    enabled: bool
    available: bool
    never_ci: bool
    rules_ready: bool


@dataclass(frozen=True)
class ActivationReadiness:
    binding_identity_valid: bool
    prompt_ready: bool
    targets: tuple[ActivationTarget, ...]


class GitLabLifecycleStore(Protocol):
    async def workspace_exists(self, workspace_id: UUID, *, lock: bool = False) -> bool: ...

    async def connection_snapshot(
        self, installation_id: UUID, *, lock: bool = False
    ) -> ConnectionSnapshot | None: ...

    async def activation_readiness(self, snapshot: ConnectionSnapshot) -> ActivationReadiness: ...

    async def enable_intake(self, snapshot: ConnectionSnapshot) -> ConnectionSnapshot: ...

    async def rotate_credential(
        self, snapshot: ConnectionSnapshot, credential: CredentialEnvelope
    ) -> ConnectionSnapshot: ...

    async def workspace_connections(self, workspace_id: UUID) -> tuple[ConnectionSnapshot, ...]: ...

    async def revoke_connections(
        self, snapshots: tuple[ConnectionSnapshot, ...]
    ) -> tuple[ConnectionSnapshot, ...]: ...

    async def inspect_connection(self, installation_id: UUID) -> ConnectionInspection | None: ...


class GitLabLifecycleUnitOfWork(UnitOfWork, Protocol):
    @property
    def connections(self) -> GitLabLifecycleStore: ...


class GitLabConnectionLifecycle:
    """Trusted operator lifecycle; containment operations need no crypto or HTTP."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], GitLabLifecycleUnitOfWork],
        codec: GitLabCredentialCodec | None = None,
        projects: GitLabProjectAccess | None = None,
        origin_policy: GitLabOriginPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._codec = codec
        self._projects = projects
        self._origins = origin_policy

    async def activate(
        self, *, installation_id: UUID, expected_generation: int
    ) -> ConnectionResult:
        """Reauthenticate stored authority, then enable only after locked readiness."""
        if not isinstance(installation_id, UUID) or (
            type(expected_generation) is not int or expected_generation < 1
        ):
            raise GitLabManagementError("invalid_identity")
        if self._codec is None or self._projects is None or self._origins is None:
            raise GitLabManagementError("activation_not_configured")
        async with self._uow_factory() as uow:
            snapshot = await uow.connections.connection_snapshot(installation_id)
        if snapshot is None:
            raise GitLabManagementError("connection_not_found")
        _require_active(snapshot)
        if snapshot.generation != expected_generation:
            raise GitLabManagementError("connection_changed")
        if not snapshot.bindings:
            raise GitLabManagementError("activation_no_bindings")
        origin = self._origins.validate(snapshot.instance_origin)
        assert snapshot.credential is not None
        token = self._codec.decrypt(
            snapshot.credential,
            installation_id=installation_id,
            workspace_id=snapshot.workspace_id,
            instance_origin=origin,
        )
        await self._projects.authenticate(origin=origin, token=token)
        for project_id, _ in snapshot.bindings:
            project = await self._projects.fetch_project(
                origin=origin, token=token, project_id=project_id
            )
            validate_project_snapshot(project.snapshot, origin=origin, project_id=project_id)
        async with self._uow_factory() as uow:
            if not await uow.connections.workspace_exists(snapshot.workspace_id, lock=True):
                raise GitLabManagementError("workspace_not_found")
            locked = await uow.connections.connection_snapshot(installation_id, lock=True)
            if locked != snapshot:
                raise GitLabManagementError("connection_changed")
            _require_active(locked)
            readiness = await uow.connections.activation_readiness(locked)
            if not readiness.binding_identity_valid:
                raise GitLabManagementError("connection_changed")
            if not readiness.prompt_ready:
                raise GitLabManagementError(
                    "prompt_not_seeded",
                    remediation="run uv run python -m app.bootstrap.seed_prompts",
                )
            if not any(
                target.enabled and target.available and target.never_ci and target.rules_ready
                for target in readiness.targets
            ):
                raise GitLabManagementError("activation_not_ready")
            updated = await uow.connections.enable_intake(locked)
            await uow.commit()
        return _connection_result(updated)

    async def rotate_token(
        self, *, installation_id: UUID, token: str, expected_generation: int | None = None
    ) -> ConnectionResult:
        if not isinstance(installation_id, UUID) or (
            expected_generation is not None
            and (type(expected_generation) is not int or expected_generation < 1)
        ):
            raise GitLabManagementError("invalid_identity")
        if self._codec is None or self._projects is None or self._origins is None:
            raise GitLabManagementError("rotation_not_configured")
        async with self._uow_factory() as uow:
            snapshot = await uow.connections.connection_snapshot(installation_id)
        if snapshot is None:
            raise GitLabManagementError("connection_not_found")
        _require_active(snapshot)
        if expected_generation is not None and snapshot.generation != expected_generation:
            raise GitLabManagementError("connection_changed")
        origin = self._origins.validate(snapshot.instance_origin)
        credential = self._codec.encrypt(
            token,
            installation_id=installation_id,
            workspace_id=snapshot.workspace_id,
            instance_origin=origin,
        )
        await self._projects.authenticate(origin=origin, token=token)
        for project_id, _ in snapshot.bindings:
            project = await self._projects.fetch_project(
                origin=origin, token=token, project_id=project_id
            )
            validate_project_snapshot(project.snapshot, origin=origin, project_id=project_id)
        async with self._uow_factory() as uow:
            if not await uow.connections.workspace_exists(snapshot.workspace_id, lock=True):
                raise GitLabManagementError("workspace_not_found")
            locked = await uow.connections.connection_snapshot(installation_id, lock=True)
            if locked != snapshot:
                raise GitLabManagementError("connection_changed")
            _require_active(locked)
            updated = await uow.connections.rotate_credential(locked, credential)
            await uow.commit()
        return _connection_result(updated)

    async def revoke_installation(self, *, installation_id: UUID) -> ConnectionResult:
        if not isinstance(installation_id, UUID):
            raise GitLabManagementError("invalid_identity")
        async with self._uow_factory() as uow:
            snapshot = await uow.connections.connection_snapshot(installation_id)
        if snapshot is None:
            raise GitLabManagementError("connection_not_found")
        async with self._uow_factory() as uow:
            if not await uow.connections.workspace_exists(snapshot.workspace_id, lock=True):
                raise GitLabManagementError("workspace_not_found")
            locked = await uow.connections.connection_snapshot(installation_id, lock=True)
            if locked is None or locked.workspace_id != snapshot.workspace_id:
                raise GitLabManagementError("connection_changed")
            updated = await uow.connections.revoke_connections((locked,))
            await uow.commit()
        return _connection_result(updated[0])

    async def revoke_workspace(self, *, workspace_id: UUID) -> tuple[ConnectionResult, ...]:
        if not isinstance(workspace_id, UUID):
            raise GitLabManagementError("invalid_identity")
        async with self._uow_factory() as uow:
            if not await uow.connections.workspace_exists(workspace_id, lock=True):
                raise GitLabManagementError("workspace_not_found")
            snapshots = await uow.connections.workspace_connections(workspace_id)
            updated = await uow.connections.revoke_connections(snapshots)
            await uow.commit()
        return tuple(_connection_result(snapshot) for snapshot in updated)

    async def inspect(self, *, installation_id: UUID) -> ConnectionInspection:
        if not isinstance(installation_id, UUID):
            raise GitLabManagementError("invalid_identity")
        async with self._uow_factory() as uow:
            result = await uow.connections.inspect_connection(installation_id)
        if result is None:
            raise GitLabManagementError("connection_not_found")
        return result
