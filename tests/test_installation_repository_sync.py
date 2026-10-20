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
from sqlalchemy.sql.dml import Update

from app.modules.code_index.domain.enums import PurgeReason
from app.modules.repositories.application.installation_repositories import (
    RepositoryReference,
    RepositorySnapshot,
)
from app.modules.repositories.application.onboard_repository import (
    PersistedRuleVersion,
    load_default_rule_sets,
)
from app.modules.repositories.application.sync_installation_repositories import (
    InstallationIdentityChanged,
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
    installation_available: bool = True


@dataclass
class FakeInstallationRepositoryStore:
    repositories: dict[tuple[UUID, int], StoredRepository] = field(default_factory=dict)
    active_versions: dict[UUID, PersistedRuleVersion] = field(default_factory=dict)
    created_versions: int = 0
    create_error: Exception | None = None
    disable_error: Exception | None = None
    identity_changes_remaining: int = 0
    lock_attempts: int = 0
    rules_refresh_requests: list[UUID] = field(default_factory=list)
    # The repository ids `disable_installation` returns; None refuses the call.
    installation_repository_ids: list[UUID] | None = None
    delivery_recorded: bool = True
    # The removal path in call order: the delivery marker, the disables and the purges.
    removal_log: list[tuple[str, object]] = field(default_factory=list)

    async def lock_active_installation(self, provider_installation_id: UUID) -> bool:
        self.lock_attempts += 1
        if self.identity_changes_remaining:
            self.identity_changes_remaining -= 1
            raise InstallationIdentityChanged("account identity changed before row lock")
        return True

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
            stored.installation_available = True
        return stored.id

    async def record_removal_delivery(self, delivery_id: str) -> bool:
        self.removal_log.append(("delivery", delivery_id))
        return self.delivery_recorded

    async def request_rules_refresh(self, repository_id: UUID) -> None:
        self.rules_refresh_requests.append(repository_id)

    async def disable_installation(self, provider_installation_id: UUID) -> list[UUID]:
        if self.installation_repository_ids is None:
            raise AssertionError("unexpected installation deletion")
        self.removal_log.append(("disable_installation", provider_installation_id))
        return list(self.installation_repository_ids)

    async def disable_repository(
        self, provider_installation_id: UUID, external_id: int
    ) -> UUID | None:
        if self.disable_error is not None:
            raise self.disable_error
        self.removal_log.append(("disable_repository", external_id))
        stored = self.repositories.get((provider_installation_id, external_id))
        if stored is None:
            return None
        stored.installation_available = False
        return stored.id

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        self.removal_log.append(("purge", (repository_id, reason)))

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

    @property
    def index_purger(self) -> FakeInstallationRepositoryStore:
        return self.repositories

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
        external_id=101,
        full_name=full_name,
        default_branch="main",
        web_url="https://github.com/octo/api",
    )


def _reference(*, full_name: str = "octo/api") -> RepositoryReference:
    return RepositoryReference(
        external_id=101,
        full_name=full_name,
        default_branch="main",
        web_url="https://github.com/octo/api",
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
    # The connected repository gets its .review/rules.md read in the same commit (#150).
    assert store.rules_refresh_requests == [result[0].rule_version.repository_id]


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
            delivery_id="removal-delivery",
            provider_installation_id=installation_id,
            repositories=(_reference(), _reference(full_name="unknown/repository")),
        )
    )
    asyncio.run(
        sync.disable(
            delivery_id="removal-delivery",
            provider_installation_id=installation_id,
            repositories=(_reference(),),
        )
    )

    assert store.repositories[(installation_id, 101)].installation_available is False
    assert store.repositories[(installation_id, 101)].enabled is True
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
    asyncio.run(
        sync.disable(
            delivery_id="removal-delivery",
            provider_installation_id=installation_id,
            repositories=(_reference(),),
        )
    )
    asyncio.run(sync.execute(provider_installation_id=installation_id, repositories=(input_,)))

    assert store.repositories[(installation_id, 101)].enabled is True
    assert store.repositories[(installation_id, 101)].installation_available is True
    assert store.created_versions == 1


def _removal(
    store: FakeInstallationRepositoryStore,
) -> tuple[SyncInstallationRepositories, FakeInstallationRepositoryUnitOfWork]:
    uow = FakeInstallationRepositoryUnitOfWork(store)
    return SyncInstallationRepositories(uow_factory=lambda: uow, rule_sets={}), uow


def test_removal_purges_each_removed_index_in_id_order_after_the_delivery_marker() -> None:
    installation_id, low, high = uuid4(), UUID(int=1), UUID(int=2)
    store = FakeInstallationRepositoryStore()
    for repository_id, external_id in ((high, 101), (low, 102)):
        store.repositories[(installation_id, external_id)] = StoredRepository(
            repository_id,
            RepositorySnapshot(external_id, f"octo/{external_id}", "main", "https://x"),
        )
    sync, uow = _removal(store)

    asyncio.run(
        sync.disable(
            delivery_id="removal-delivery",
            provider_installation_id=installation_id,
            repositories=tuple(
                RepositoryReference(external_id, f"octo/{external_id}", "main", "https://x")
                for external_id in (101, 102, 999)
            ),
        )
    )

    # After the repositories rows, in the same commit and in id order (#143).
    assert store.removal_log == [
        ("delivery", "removal-delivery"),
        ("disable_repository", 101),
        ("disable_repository", 102),
        ("disable_repository", 999),
        ("purge", (low, PurgeReason.REMOVED)),
        ("purge", (high, PurgeReason.REMOVED)),
    ]
    assert uow.commits == 1


def test_installation_deletion_purges_every_index_of_the_installation_in_id_order() -> None:
    installation_id = uuid4()
    store = FakeInstallationRepositoryStore(
        installation_repository_ids=[UUID(int=3), UUID(int=1), UUID(int=2)]
    )
    sync, uow = _removal(store)

    asyncio.run(
        sync.disable(
            delivery_id="deletion-delivery",
            provider_installation_id=installation_id,
            repositories=(),
            all_repositories=True,
        )
    )

    assert store.removal_log == [
        ("delivery", "deletion-delivery"),
        ("disable_installation", installation_id),
        *(("purge", (UUID(int=number), PurgeReason.REMOVED)) for number in (1, 2, 3)),
    ]
    assert uow.commits == 1


def test_a_repeated_removal_delivery_purges_nothing() -> None:
    store = FakeInstallationRepositoryStore(delivery_recorded=False)
    sync, uow = _removal(store)

    asyncio.run(
        sync.disable(
            delivery_id="removal-delivery",
            provider_installation_id=uuid4(),
            repositories=(_reference(),),
        )
    )

    assert store.removal_log == [("delivery", "removal-delivery")]
    assert uow.commits == 0


def test_sync_rolls_back_and_never_commits_when_soft_disable_fails() -> None:
    store = FakeInstallationRepositoryStore(disable_error=RuntimeError("database unavailable"))
    uow = FakeInstallationRepositoryUnitOfWork(store)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(
            SyncInstallationRepositories(
                uow_factory=lambda: uow,
                rule_sets=load_default_rule_sets(Path("review/rules")),
            ).disable(
                delivery_id="removal-delivery",
                provider_installation_id=uuid4(),
                repositories=(_reference(),),
            )
        )

    assert uow.commits == 0
    assert uow.rollbacks == 1


class FakeSqlAlchemySession:
    def __init__(self) -> None:
        self.repository: Repository | None = None
        self.flushes = 0

    async def get(self, model: object, identity: UUID, **kwargs: object) -> None:
        return None

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
    # What the repository UPDATE returns.
    repository_ids: list[UUID] = field(default_factory=list)

    async def execute(self, statement: object) -> None:
        self.statements.append(statement)

    async def scalar(self, statement: object) -> UUID | None:
        self.statements.append(statement)
        return self.repository_ids[0] if self.repository_ids else None

    async def scalars(self, statement: object) -> list[UUID]:
        self.statements.append(statement)
        return list(self.repository_ids)


def _repository_updates(session: FakeDisableSession) -> list[Update]:
    return [
        statement
        for statement in session.statements
        if isinstance(statement, Update) and statement.table.compare(Repository.__table__)
    ]


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
    installation_id, repository_id = uuid4(), uuid4()
    session = FakeDisableSession(repository_ids=[repository_id])
    store = SqlAlchemyInstallationRepositoryStore(session)  # type: ignore[arg-type]

    disabled = asyncio.run(store.disable_repository(installation_id, 101))

    repository_updates = _repository_updates(session)
    assert len(repository_updates) == 1
    compiled = repository_updates[0].compile()
    assert compiled.params["installation_available"] is False
    assert "enabled" not in compiled.params
    assert compiled.params["provider_installation_id_1"] == installation_id
    assert compiled.params["external_id_1"] == 101
    # The use case purges the index by this id (#143).
    assert disabled == repository_id


def test_sqlalchemy_installation_store_returns_every_repository_of_a_deleted_installation() -> None:
    installation_id, repository_ids = uuid4(), [uuid4(), uuid4()]
    session = FakeDisableSession(repository_ids=repository_ids)
    store = SqlAlchemyInstallationRepositoryStore(session)  # type: ignore[arg-type]

    disabled = asyncio.run(store.disable_installation(installation_id))

    repository_updates = _repository_updates(session)
    assert len(repository_updates) == 1
    compiled = repository_updates[0].compile()
    assert compiled.params["installation_available"] is False
    assert compiled.params["provider_installation_id_1"] == installation_id
    assert disabled == repository_ids


def test_disable_without_delivery_id_fails_before_opening_uow() -> None:
    opened = 0

    def uow_factory() -> FakeInstallationRepositoryUnitOfWork:
        nonlocal opened
        opened += 1
        return FakeInstallationRepositoryUnitOfWork(FakeInstallationRepositoryStore())

    sync = SyncInstallationRepositories(uow_factory=uow_factory, rule_sets={})
    with pytest.raises(ValueError, match="removal requires a durable delivery id"):
        asyncio.run(
            sync.disable(
                provider_installation_id=uuid4(),
                repositories=(_reference(),),
                delivery_id="",
            )
        )

    assert opened == 0


@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize("changes", [1, 2])
def test_sync_identity_transition_retries_once_and_reports_installation(
    preflight: bool, changes: int, caplog: pytest.LogCaptureFixture
) -> None:
    installation = UUID(int=11313)
    store = FakeInstallationRepositoryStore(identity_changes_remaining=changes)
    uow = FakeInstallationRepositoryUnitOfWork(store)
    sync = SyncInstallationRepositories(uow_factory=lambda: uow, rule_sets={})

    async def exercise() -> bool | tuple[object, ...]:
        if preflight:
            return await sync.is_active(installation)
        return await sync.execute(provider_installation_id=installation, repositories=())

    with caplog.at_level("INFO"):
        if changes == 2:
            with pytest.raises(InstallationIdentityChanged):
                asyncio.run(exercise())
        else:
            assert asyncio.run(exercise()) == (True if preflight else ())
    assert store.lock_attempts == 2
    assert uow.rollbacks == changes
    assert uow.commits == (1 if changes == 1 and not preflight else 0)
    assert str(installation) in caplog.text
    assert "account identity changed before row lock" in caplog.text
