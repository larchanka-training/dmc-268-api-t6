"""Behavioural contracts for atomic installation repository onboarding."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import (
    PersistedRuleVersion,
    load_default_rule_sets,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoryStore,
)
from app.modules.repositories.infrastructure.models import Repository


@dataclass
class StoredRepository:
    id: UUID
    snapshot: RepositorySnapshot
    enabled: bool = True


@dataclass
class FakeInstallationRepositoryStore:
    repositories: dict[tuple[UUID, int], StoredRepository] = field(default_factory=dict)
    active_versions: dict[UUID, PersistedRuleVersion] = field(default_factory=dict)
    created_versions: int = 0
    create_error: Exception | None = None
    disable_error: Exception | None = None

    async def upsert_repository(
        self, provider_installation_id: UUID, snapshot: RepositorySnapshot
    ) -> UUID:
        key = (provider_installation_id, snapshot.external_id)
        stored = self.repositories.get(key)
        if stored is None:
            stored = StoredRepository(id=uuid4(), snapshot=snapshot)
            self.repositories[key] = stored
        else:
            stored.snapshot = snapshot
            stored.enabled = True
        return stored.id

    async def disable_repository(self, provider_installation_id: UUID, external_id: int) -> None:
        if self.disable_error is not None:
            raise self.disable_error
        stored = self.repositories.get((provider_installation_id, external_id))
        if stored is not None:
            stored.enabled = False

    async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None:
        return self.active_versions.get(repository_id)

    async def get_or_create_initial_rule_version(
        self, repository_id: UUID, version: int, rules: list[dict[str, object]]
    ) -> PersistedRuleVersion:
        if self.create_error is not None:
            raise self.create_error
        existing = self.active_versions.get(repository_id)
        if existing is not None:
            return existing
        result = PersistedRuleVersion(
            repository_id=repository_id,
            version=version,
            rules=rules,
            checksum=sha256(
                json.dumps(rules, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )
        self.active_versions[repository_id] = result
        self.created_versions += 1
        return result


@dataclass
class FakeInstallationRepositoryUnitOfWork:
    repositories: FakeInstallationRepositoryStore
    commits: int = 0
    rollbacks: int = 0

    async def __aenter__(self) -> FakeInstallationRepositoryUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        if exc_type is not None:
            self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _snapshot(*, full_name: str = "octo/api") -> RepositorySnapshot:
    return RepositorySnapshot(
        id=101,
        full_name=full_name,
        default_branch="main",
        html_url="https://github.com/octo/api",
    )


def test_sync_creates_repository_and_its_one_active_default_in_one_commit() -> None:
    store = FakeInstallationRepositoryStore()
    uow = FakeInstallationRepositoryUnitOfWork(store)
    result = asyncio.run(
        SyncInstallationRepositories(
            uow_factory=lambda: uow,
            rule_sets=load_default_rule_sets(Path("review/rules")),
        ).execute(
            provider_installation_id=uuid4(),
            repositories=(
                RepositoryOnboardingInput(snapshot=_snapshot(), languages={"TypeScript": 100}),
            ),
        )
    )

    assert len(result) == 1
    assert result[0].created is True
    assert result[0].rule_version.rules[-1]["name"] == "FSD Layer Boundaries"
    assert len(store.repositories) == 1
    assert store.created_versions == 1
    assert uow.commits == 1


def test_sync_replay_updates_metadata_but_keeps_the_immutable_active_version() -> None:
    installation_id = uuid4()
    store = FakeInstallationRepositoryStore()
    uow = FakeInstallationRepositoryUnitOfWork(store)
    sync = SyncInstallationRepositories(
        uow_factory=lambda: uow,
        rule_sets=load_default_rule_sets(Path("review/rules")),
    )

    first = asyncio.run(
        sync.execute(
            provider_installation_id=installation_id,
            repositories=(
                RepositoryOnboardingInput(snapshot=_snapshot(), languages={"Python": 100}),
            ),
        )
    )
    second = asyncio.run(
        sync.execute(
            provider_installation_id=installation_id,
            repositories=(
                RepositoryOnboardingInput(
                    snapshot=_snapshot(full_name="octo/renamed-api"),
                    languages={"TypeScript": 100},
                ),
            ),
        )
    )

    stored = store.repositories[(installation_id, 101)]
    assert second[0].created is False
    assert second[0].rule_version == first[0].rule_version
    assert stored.snapshot.full_name == "octo/renamed-api"
    assert store.created_versions == 1
    assert uow.commits == 2


def test_sync_rolls_back_and_never_commits_when_onboarding_fails() -> None:
    store = FakeInstallationRepositoryStore(create_error=RuntimeError("database unavailable"))
    uow = FakeInstallationRepositoryUnitOfWork(store)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(
            SyncInstallationRepositories(
                uow_factory=lambda: uow,
                rule_sets=load_default_rule_sets(Path("review/rules")),
            ).execute(
                provider_installation_id=uuid4(),
                repositories=(
                    RepositoryOnboardingInput(snapshot=_snapshot(), languages={"Python": 100}),
                ),
            )
        )

    assert uow.commits == 0
    assert uow.rollbacks == 1


def test_sync_soft_disables_only_matching_repositories_idempotently() -> None:
    installation_id = uuid4()
    store = FakeInstallationRepositoryStore()
    uow = FakeInstallationRepositoryUnitOfWork(store)
    sync = SyncInstallationRepositories(
        uow_factory=lambda: uow,
        rule_sets=load_default_rule_sets(Path("review/rules")),
    )
    asyncio.run(
        sync.execute(
            provider_installation_id=installation_id,
            repositories=(
                RepositoryOnboardingInput(snapshot=_snapshot(), languages={"Python": 100}),
            ),
        )
    )

    asyncio.run(
        sync.disable(
            provider_installation_id=installation_id,
            repositories=(_snapshot(), _snapshot(full_name="unknown/repository")),
        )
    )
    asyncio.run(sync.disable(provider_installation_id=installation_id, repositories=(_snapshot(),)))

    assert store.repositories[(installation_id, 101)].enabled is False
    assert uow.commits == 3


def test_sync_readding_a_removed_repository_reenables_it() -> None:
    """An ``added`` delivery restores a repository disabled by ``removed``."""
    installation_id = uuid4()
    store = FakeInstallationRepositoryStore()
    uow = FakeInstallationRepositoryUnitOfWork(store)
    sync = SyncInstallationRepositories(
        uow_factory=lambda: uow,
        rule_sets=load_default_rule_sets(Path("review/rules")),
    )
    input_ = RepositoryOnboardingInput(snapshot=_snapshot(), languages={"Python": 100})

    asyncio.run(sync.execute(provider_installation_id=installation_id, repositories=(input_,)))
    asyncio.run(sync.disable(provider_installation_id=installation_id, repositories=(_snapshot(),)))
    asyncio.run(sync.execute(provider_installation_id=installation_id, repositories=(input_,)))

    assert store.repositories[(installation_id, 101)].enabled is True
    assert store.created_versions == 1


def test_sync_rolls_back_and_never_commits_when_soft_disable_fails() -> None:
    store = FakeInstallationRepositoryStore(disable_error=RuntimeError("database unavailable"))
    uow = FakeInstallationRepositoryUnitOfWork(store)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(
            SyncInstallationRepositories(
                uow_factory=lambda: uow,
                rule_sets=load_default_rule_sets(Path("review/rules")),
            ).disable(
                provider_installation_id=uuid4(),
                repositories=(_snapshot(),),
            )
        )

    assert uow.commits == 0
    assert uow.rollbacks == 1


class FakeSqlAlchemySession:
    def __init__(self) -> None:
        self.repository: Repository | None = None
        self.flushes = 0

    async def scalar(self, statement: object) -> UUID:
        params = cast(dict[str, object], cast(Any, statement).compile().params)
        if self.repository is None:
            self.repository = Repository(
                provider_installation_id=cast(UUID, params["provider_installation_id"]),
                external_id=cast(int, params["external_id"]),
                full_name=cast(str, params["full_name"]),
                default_branch=cast(str, params["default_branch"]),
                web_url=cast(str, params["web_url"]),
            )
            self.repository.id = uuid4()
        else:
            self.repository.full_name = cast(str, params["full_name"])
            self.repository.default_branch = cast(str, params["default_branch"])
            self.repository.web_url = cast(str, params["web_url"])
        return self.repository.id


@dataclass
class FakeDisableSession:
    statements: list[object] = field(default_factory=list)

    async def execute(self, statement: object) -> None:
        self.statements.append(statement)


def test_sqlalchemy_installation_store_upserts_without_session_flushes() -> None:
    installation_id = uuid4()
    session = FakeSqlAlchemySession()
    store = SqlAlchemyInstallationRepositoryStore(session)  # type: ignore[arg-type]

    repository_id = asyncio.run(store.upsert_repository(installation_id, _snapshot()))
    updated_id = asyncio.run(
        store.upsert_repository(installation_id, _snapshot(full_name="octo/renamed-api"))
    )

    assert updated_id == repository_id
    assert session.repository is not None
    assert session.repository.full_name == "octo/renamed-api"
    assert session.flushes == 0


def test_sqlalchemy_installation_store_soft_disables_matching_repository() -> None:
    installation_id = uuid4()
    session = FakeDisableSession()
    store = SqlAlchemyInstallationRepositoryStore(session)  # type: ignore[arg-type]

    asyncio.run(store.disable_repository(installation_id, 101))

    assert len(session.statements) == 1
    compiled = cast(Any, session.statements[0]).compile()
    assert compiled.params["enabled"] is False
    assert compiled.params["provider_installation_id_1"] == installation_id
    assert compiled.params["external_id_1"] == 101
