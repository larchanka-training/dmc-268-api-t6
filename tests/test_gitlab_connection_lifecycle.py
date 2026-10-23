"""Public lifecycle behavior with detached fake ports and synthetic secrets."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.modules.integrations.gitlab.application.manage_connection import (
    ActivationReadiness,
    ActivationTarget,
    ConnectionInspection,
    ConnectionResult,
    ConnectionSnapshot,
    CredentialEnvelope,
    GitLabConnectionLifecycle,
    GitLabManagementError,
    GitLabOriginPolicy,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)

_WORKSPACE = UUID(int=1)
_INSTALLATION = UUID(int=2)
_REPOSITORY = UUID(int=3)
_ORIGIN = "https://gitlab.com"


class Store:
    snapshot = ConnectionSnapshot(
        _INSTALLATION,
        _WORKSPACE,
        _ORIGIN,
        4,
        CredentialEnvelope("lost-old-key", b"old"),
        bindings=((42, _REPOSITORY),),
    )
    active = False
    commits = 0

    async def workspace_exists(self, workspace_id: UUID, *, lock: bool = False) -> bool:
        return workspace_id == _WORKSPACE

    async def connection_snapshot(
        self, installation_id: UUID, *, lock: bool = False
    ) -> ConnectionSnapshot | None:
        return self.snapshot if installation_id == _INSTALLATION else None

    async def activation_readiness(self, snapshot: ConnectionSnapshot) -> ActivationReadiness:
        assert self.active
        return ActivationReadiness(
            True, True, (ActivationTarget(_REPOSITORY, 42, True, True, True, True),)
        )

    async def enable_intake(self, snapshot: ConnectionSnapshot) -> ConnectionSnapshot:
        self.snapshot = replace(snapshot, intake_enabled=True)
        return self.snapshot

    async def rotate_credential(
        self, snapshot: ConnectionSnapshot, credential: CredentialEnvelope
    ) -> ConnectionSnapshot:
        self.snapshot = replace(snapshot, generation=snapshot.generation + 1, credential=credential)
        return self.snapshot

    async def workspace_connections(self, workspace_id: UUID) -> tuple[ConnectionSnapshot, ...]:
        return (self.snapshot,)

    async def revoke_connections(
        self, snapshots: tuple[ConnectionSnapshot, ...]
    ) -> tuple[ConnectionSnapshot, ...]:
        if not self.snapshot.revoked:
            self.snapshot = replace(
                self.snapshot,
                revoked=True,
                credential=None,
                generation=self.snapshot.generation + 1,
                intake_enabled=False,
                bindings=(),
            )
        return (self.snapshot,)

    async def inspect_connection(self, installation_id: UUID) -> ConnectionInspection | None:
        snapshot = await self.connection_snapshot(installation_id)
        if snapshot is None:
            return None
        return ConnectionInspection(
            ConnectionResult(
                snapshot.installation_id,
                snapshot.workspace_id,
                snapshot.instance_origin,
                snapshot.generation,
                snapshot.intake_enabled,
                snapshot.revoked,
            ),
            (),
            True,
        )


class Uow:
    def __init__(self, store: Store) -> None:
        self.connections = store

    async def __aenter__(self) -> Self:
        assert not self.connections.active
        self.connections.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.connections.active = False

    async def commit(self) -> None:
        self.connections.commits += 1

    async def rollback(self) -> None:
        pass


class Codec:
    def encrypt(
        self, token: str, *, installation_id: UUID, workspace_id: UUID, instance_origin: str
    ) -> CredentialEnvelope:
        assert token == "replacement-token"
        return CredentialEnvelope("new-key", b"new-authenticated-envelope")

    def decrypt(
        self,
        credential: CredentialEnvelope,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
    ) -> str:
        raise AssertionError("rotation must not decrypt old credential")

    def webhook_digest(self, secret: str) -> str:
        raise AssertionError("rotation must preserve webhook digest")


class Projects:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.validated: list[int] = []

    async def authenticate(self, *, origin: str, token: str) -> None:
        assert not self.store.active
        assert origin == _ORIGIN
        assert token == "replacement-token"

    async def fetch_project(
        self, *, origin: str, token: str, project_id: int
    ) -> RepositoryOnboardingInput:
        await self.authenticate(origin=origin, token=token)
        self.validated.append(project_id)
        return RepositoryOnboardingInput(
            RepositorySnapshot(project_id, "group/project", "main", f"{origin}/group/project"),
            {"Python": 100},
        )


def test_rotation_validates_bound_projects_outside_uow_without_old_key() -> None:
    store = Store()
    projects = Projects(store)
    lifecycle = GitLabConnectionLifecycle(
        uow_factory=lambda: Uow(store),
        codec=Codec(),
        projects=projects,
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
    )
    result = asyncio.run(
        lifecycle.rotate_token(
            installation_id=_INSTALLATION,
            token="replacement-token",
            expected_generation=4,
        )
    )
    assert result.generation == 5
    assert result.revoked is False
    assert store.snapshot.credential == CredentialEnvelope("new-key", b"new-authenticated-envelope")
    assert store.snapshot.bindings == ((42, _REPOSITORY),)
    assert projects.validated == [42]
    assert store.commits == 1


def test_revoke_and_inspect_are_available_without_crypto_or_network() -> None:
    store = Store()
    lifecycle = GitLabConnectionLifecycle(uow_factory=lambda: Uow(store))
    result = asyncio.run(lifecycle.revoke_installation(installation_id=_INSTALLATION))
    assert result.revoked is True
    assert result.generation == 5
    assert store.snapshot.credential is None
    again = asyncio.run(lifecycle.revoke_installation(installation_id=_INSTALLATION))
    assert again == result
    inspected = asyncio.run(lifecycle.inspect(installation_id=_INSTALLATION))
    assert inspected.connection == result
    assert inspected.repositories == ()
    assert "lost-old-key" not in repr(inspected)
    assert "old" not in repr(inspected)


@pytest.mark.parametrize("race", ["generation", "binding", "revoke", "origin"])
def test_rotation_refuses_stale_validation_without_committing(race: str) -> None:
    store = Store()

    class RacingProjects(Projects):
        async def fetch_project(
            self, *, origin: str, token: str, project_id: int
        ) -> RepositoryOnboardingInput:
            result = await super().fetch_project(origin=origin, token=token, project_id=project_id)
            if race == "generation":
                store.snapshot = replace(store.snapshot, generation=5)
            elif race == "binding":
                store.snapshot = replace(
                    store.snapshot, bindings=((42, _REPOSITORY), (99, UUID(int=9)))
                )
            elif race == "origin":
                store.snapshot = replace(
                    store.snapshot, instance_origin="https://other.example.com"
                )
            else:
                store.snapshot = replace(
                    store.snapshot, revoked=True, credential=None, generation=5
                )
            return result

    lifecycle = GitLabConnectionLifecycle(
        uow_factory=lambda: Uow(store),
        codec=Codec(),
        projects=RacingProjects(store),
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
    )
    with pytest.raises(GitLabManagementError, match="connection_changed"):
        asyncio.run(
            lifecycle.rotate_token(installation_id=_INSTALLATION, token="replacement-token")
        )
    assert store.commits == 0
    assert store.snapshot.credential != CredentialEnvelope("new-key", b"new-authenticated-envelope")


@pytest.mark.parametrize("stage", ["authenticate", "project"])
def test_invalid_replacement_cannot_change_credentials(stage: str) -> None:
    store = Store()
    original = store.snapshot

    class RejectedProjects(Projects):
        async def authenticate(self, *, origin: str, token: str) -> None:
            await super().authenticate(origin=origin, token=token)
            if stage == "authenticate":
                raise GitLabManagementError("invalid_token")

        async def fetch_project(
            self, *, origin: str, token: str, project_id: int
        ) -> RepositoryOnboardingInput:
            raise GitLabManagementError("project_access_failed")

    lifecycle = GitLabConnectionLifecycle(
        uow_factory=lambda: Uow(store),
        codec=Codec(),
        projects=RejectedProjects(store),
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
    )
    with pytest.raises(GitLabManagementError):
        asyncio.run(
            lifecycle.rotate_token(installation_id=_INSTALLATION, token="replacement-token")
        )
    assert store.snapshot == original
    assert store.commits == 0


@pytest.mark.parametrize("generation", [0, True, -1, "4"])
def test_rotation_rejects_invalid_generation(generation: object) -> None:
    lifecycle = GitLabConnectionLifecycle(uow_factory=lambda: Uow(Store()))
    with pytest.raises(GitLabManagementError, match="invalid_identity"):
        asyncio.run(
            lifecycle.rotate_token(
                installation_id=_INSTALLATION,
                token="replacement-token",
                expected_generation=generation,  # type: ignore[arg-type]
            )
        )


def test_expected_generation_is_checked_before_provider_and_workspace_revoke_is_keyless() -> None:
    store = Store()
    lifecycle = GitLabConnectionLifecycle(
        uow_factory=lambda: Uow(store),
        codec=Codec(),
        projects=Projects(store),
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
    )
    with pytest.raises(GitLabManagementError, match="connection_changed"):
        asyncio.run(
            lifecycle.rotate_token(
                installation_id=_INSTALLATION,
                token="replacement-token",
                expected_generation=3,
            )
        )
    keyless = GitLabConnectionLifecycle(uow_factory=lambda: Uow(store))
    results = asyncio.run(keyless.revoke_workspace(workspace_id=_WORKSPACE))
    assert len(results) == 1
    assert results[0].generation == 5
    assert results[0].revoked is True
    assert store.commits == 1


class StoredCodec(Codec):
    def decrypt(
        self,
        credential: CredentialEnvelope,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
    ) -> str:
        assert (installation_id, workspace_id, instance_origin) == (
            _INSTALLATION,
            _WORKSPACE,
            _ORIGIN,
        )
        assert credential == CredentialEnvelope("lost-old-key", b"old")
        return "replacement-token"


def test_activation_enables_stored_connection_without_rotating_or_rebinding() -> None:
    store = Store()
    projects = Projects(store)
    lifecycle = GitLabConnectionLifecycle(
        uow_factory=lambda: Uow(store),
        codec=StoredCodec(),
        projects=projects,
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
    )
    original = store.snapshot
    result = asyncio.run(lifecycle.activate(installation_id=_INSTALLATION, expected_generation=4))
    assert result.intake_enabled is True
    assert result.generation == 4
    assert store.snapshot == replace(original, intake_enabled=True)
    assert projects.validated == [42]
    assert store.commits == 1


@pytest.mark.parametrize("generation", [0, True])
def test_activation_requires_a_positive_nonboolean_generation(generation: int) -> None:
    lifecycle = GitLabConnectionLifecycle(uow_factory=lambda: Uow(Store()))
    with pytest.raises(GitLabManagementError, match="invalid_identity"):
        asyncio.run(
            lifecycle.activate(installation_id=_INSTALLATION, expected_generation=generation)
        )


@pytest.mark.parametrize(
    "condition,code",
    [
        ("stale", "connection_changed"),
        ("empty", "activation_no_bindings"),
        ("revoked", "connection_revoked"),
        ("unconfigured", "activation_not_configured"),
        ("identity", "connection_changed"),
    ],
)
def test_activation_fails_closed_before_any_write(condition: str, code: str) -> None:
    class ActivationStore(Store):
        async def activation_readiness(self, snapshot: ConnectionSnapshot) -> ActivationReadiness:
            return ActivationReadiness(False, True, ())

    store = ActivationStore()
    if condition == "empty":
        store.snapshot = replace(store.snapshot, bindings=())
    elif condition == "revoked":
        store.snapshot = replace(store.snapshot, revoked=True)
    original = store.snapshot
    projects = Projects(store)
    lifecycle = GitLabConnectionLifecycle(
        uow_factory=lambda: Uow(store),
        codec=None if condition == "unconfigured" else StoredCodec(),
        projects=projects,
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
    )
    with pytest.raises(GitLabManagementError, match=code):
        asyncio.run(
            lifecycle.activate(
                installation_id=_INSTALLATION,
                expected_generation=3 if condition == "stale" else 4,
            )
        )
    assert store.snapshot == original
    assert store.commits == 0
    assert projects.validated == ([42] if condition == "identity" else [])
