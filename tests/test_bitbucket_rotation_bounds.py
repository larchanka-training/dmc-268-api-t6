"""Bounded provider work while replacing a Bitbucket installation token."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Self, cast
from uuid import UUID

import pytest

from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application import manage_connection as management
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketCredentialCodec,
    BitbucketManagementStore,
    BitbucketManagementUnitOfWork,
    BitbucketRepositoryProvider,
    BitbucketRotationProbeError,
    CredentialRotateResult,
    InstallationSnapshot,
    ManageBitbucketConnection,
    RepositoryBinding,
)
from app.modules.integrations.bitbucket.application.provider_error import BitbucketProviderError

WORKSPACE = UUID(int=101)
INSTALLATION = UUID(int=102)
REMOTE_WORKSPACE = UUID(int=103)
SCOPE = AuthScope(42, (WORKSPACE,))


@dataclass
class _State:
    bindings: tuple[RepositoryBinding, ...]
    in_transaction: bool = False
    commits: int = 0
    generation: int = 1
    metadata_updates: list[UUID] = field(default_factory=list)


class _Store:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def is_member(self, *, user_id: int, workspace_id: UUID, lock: bool = False) -> bool:
        assert self.state.in_transaction
        assert (user_id, workspace_id) == (42, WORKSPACE)
        return True

    async def installation_by_id(
        self, *, workspace_id: UUID, installation_id: UUID
    ) -> InstallationSnapshot:
        assert self.state.in_transaction
        assert (workspace_id, installation_id) == (WORKSPACE, INSTALLATION)
        return InstallationSnapshot(
            INSTALLATION, self.state.generation, REMOTE_WORKSPACE, "key", b"old"
        )

    async def list_bindings(
        self, *, workspace_id: UUID, installation_id: UUID | None = None
    ) -> tuple[RepositoryBinding, ...]:
        assert self.state.in_transaction
        assert workspace_id == WORKSPACE
        assert installation_id == INSTALLATION
        return self.state.bindings

    async def rotate_token(
        self, *, workspace_id: UUID, installation_id: UUID, token: object
    ) -> int:
        assert self.state.in_transaction
        assert (workspace_id, installation_id) == (WORKSPACE, INSTALLATION)
        self.state.generation += 1
        return self.state.generation

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
        assert self.state.in_transaction
        assert (workspace_id, installation_id) == (WORKSPACE, INSTALLATION)
        self.state.metadata_updates.append(repository_id)


class _Uow:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.store = _Store(state)

    @property
    def connections(self) -> BitbucketManagementStore:
        return cast(BitbucketManagementStore, self.store)

    async def __aenter__(self) -> Self:
        assert not self.state.in_transaction
        self.state.in_transaction = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.state.in_transaction = False

    async def commit(self) -> None:
        assert self.state.in_transaction
        self.state.commits += 1

    async def rollback(self) -> None:
        assert self.state.in_transaction


class _Cipher:
    def encrypt_token(self, token: str, *, installation_id: UUID, workspace_id: UUID) -> object:
        assert (token, installation_id, workspace_id) == ("new-secret", INSTALLATION, WORKSPACE)
        return SimpleNamespace(key_id="key", ciphertext=b"new")


class _Provider:
    def __init__(
        self,
        state: _State,
        *,
        delay: float,
        slow_uuid: UUID | None = None,
        fail_uuid: UUID | None = None,
        blocked_uuids: frozenset[UUID] = frozenset(),
    ) -> None:
        self.state = state
        self.delay = delay
        self.slow_uuid = slow_uuid
        self.fail_uuid = fail_uuid
        self.blocked_uuids = blocked_uuids
        self.blocked = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.fetch_calls = 0
        self.pr_probes = 0

    async def probe_current_user(self, *, token: str) -> None:
        assert not self.state.in_transaction
        assert token == "new-secret"

    async def fetch_repository_by_uuid(
        self, *, workspace_uuid: UUID, repository_uuid: UUID, token: str
    ) -> object:
        assert not self.state.in_transaction
        assert (workspace_uuid, token) == (REMOTE_WORKSPACE, "new-secret")
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.fetch_calls += 1
        try:
            if repository_uuid in self.blocked_uuids:
                await self.blocked.wait()
            else:
                await asyncio.sleep(5 if repository_uuid == self.slow_uuid else self.delay)
            if repository_uuid == self.fail_uuid:
                raise BitbucketProviderError("repository_inaccessible")
            return SimpleNamespace(
                workspace_uuid=REMOTE_WORKSPACE,
                full_name=f"team/repo-{repository_uuid.int}",
                default_branch="main",
                html_url=f"https://bitbucket.org/team/repo-{repository_uuid.int}",
            )
        finally:
            self.active -= 1

    async def probe_pull_request_read(
        self, *, workspace_uuid: UUID, repository_uuid: UUID, token: str
    ) -> None:
        assert not self.state.in_transaction
        assert (workspace_uuid, token) == (REMOTE_WORKSPACE, "new-secret")
        self.pr_probes += 1


def _manager(state: _State, provider: _Provider) -> ManageBitbucketConnection:
    return ManageBitbucketConnection(
        uow_factory=lambda: cast(BitbucketManagementUnitOfWork, _Uow(state)),
        cipher=cast(BitbucketCredentialCodec, _Cipher()),
        provider=cast(BitbucketRepositoryProvider, provider),
        rule_sets={},
        webhook_base_url="https://portal.example",
    )


def _state(count: int = 8) -> _State:
    return _State(
        tuple(
            RepositoryBinding(
                INSTALLATION,
                UUID(int=200 + index),
                UUID(int=300 + index),
                f"team/repo-{index}",
                f"https://bitbucket.org/team/repo-{index}",
                1,
                1,
                None,
            )
            for index in range(count)
        )
    )


def _rotate(manager: ManageBitbucketConnection) -> CredentialRotateResult:
    async def exercise() -> CredentialRotateResult:
        return await asyncio.wait_for(
            manager.rotate_token(
                scope=SCOPE,
                workspace_id=WORKSPACE,
                installation_id=INSTALLATION,
                token="new-secret",
            ),
            timeout=3,
        )

    return asyncio.run(exercise())


def test_rotation_probes_all_repositories_with_at_most_four_active() -> None:
    state = _state()
    provider = _Provider(state, delay=0.03)

    result = _rotate(_manager(state, provider))

    assert result.generation == 2
    assert (provider.fetch_calls, provider.pr_probes, provider.peak) == (8, 8, 4)
    assert state.commits == 1
    assert len(state.metadata_updates) == 8


def test_rotation_deadline_covers_all_batches_and_drains_pending_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    blocked_uuids = frozenset(binding.repository_uuid for binding in state.bindings[4:])
    provider = _Provider(state, delay=0, blocked_uuids=blocked_uuids)
    monkeypatch.setattr(management, "_ROTATION_DEADLINE_SECONDS", 1)

    with pytest.raises(BitbucketRotationProbeError) as failure:
        _rotate(_manager(state, provider))

    assert failure.value.code == "provider_unavailable"
    assert failure.value.repository_uuid in blocked_uuids
    assert provider.fetch_calls == 8
    assert provider.pr_probes == 4
    assert provider.peak <= 4
    assert provider.active == 0
    assert (state.commits, state.generation, state.metadata_updates) == (0, 1, [])


def test_one_slow_repository_is_identified_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(4)
    slow_uuid = state.bindings[2].repository_uuid
    provider = _Provider(state, delay=0, slow_uuid=slow_uuid)
    monkeypatch.setattr(management, "_ROTATION_DEADLINE_SECONDS", 1)

    with pytest.raises(BitbucketRotationProbeError) as failure:
        _rotate(_manager(state, provider))

    assert failure.value.code == "provider_unavailable"
    assert failure.value.repository_uuid == slow_uuid
    assert provider.pr_probes == 3
    assert provider.peak <= 4
    assert provider.active == 0
    assert state.commits == 0


def test_rotation_failure_names_repository_without_exposing_credential() -> None:
    state = _state()
    failing_uuid = state.bindings[1].repository_uuid
    provider = _Provider(state, delay=0.03, fail_uuid=failing_uuid)

    with pytest.raises(BitbucketRotationProbeError) as failure:
        _rotate(_manager(state, provider))

    assert failure.value.code == "repository_inaccessible"
    assert failure.value.repository_uuid == failing_uuid
    assert "new-secret" not in str(failure.value)
    assert "bitbucket.org" not in str(failure.value)
    assert provider.fetch_calls <= 4
    assert provider.peak <= 4
    assert provider.active == 0
    assert (state.commits, state.generation, state.metadata_updates) == (0, 1, [])
