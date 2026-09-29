"""Behavioural contracts for projecting durable installation events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4

import pytest

from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationEventProjector,
    InstallationRepositoryLabelProvider,
    InstallationRepositoryTreeProvider,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositorySnapshot,
    RepositoryTreeBlob,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)


def _snapshot(*, external_id: int = 101, branch: str = "main") -> RepositorySnapshot:
    return RepositorySnapshot(
        external_id=external_id,
        full_name=f"octo/repository-{external_id}",
        default_branch=branch,
        web_url=f"https://github.com/octo/repository-{external_id}",
    )


@dataclass
class FakeTreeProvider(InstallationRepositoryTreeProvider):
    trees: dict[int, tuple[RepositoryTreeBlob, ...]] = field(default_factory=dict)
    error: Exception | None = None
    calls: list[tuple[int, int, str]] = field(default_factory=list)

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        self.calls.append(
            (installation_external_id, repository.external_id, repository.default_branch)
        )
        if self.error is not None:
            raise self.error
        return self.trees[repository.external_id]


@dataclass
class FakeSyncInstallationRepositories:
    calls: list[tuple[UUID, tuple[RepositoryOnboardingInput, ...]]] = field(default_factory=list)
    disable_calls: list[tuple[UUID, tuple[RepositorySnapshot, ...]]] = field(default_factory=list)

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryOnboardingInput, ...],
    ) -> tuple[OnboardingResult, ...]:
        self.calls.append((provider_installation_id, repositories))
        return ()

    async def disable(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositorySnapshot, ...],
    ) -> None:
        self.disable_calls.append((provider_installation_id, repositories))


@dataclass
class FakeLabelProvider(InstallationRepositoryLabelProvider):
    calls: list[tuple[int, str]] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None:
        self.calls.append((installation_external_id, repository.full_name))
        if repository.full_name in self.fail_for:
            raise RuntimeError("GitHub label API unavailable")


@pytest.mark.parametrize("action", ["created", "added"])
def test_added_repositories_get_ai_review_label_before_database_sync(
    action: Literal["created", "added"],
) -> None:
    first = _snapshot(external_id=101)
    second = _snapshot(external_id=102)
    tree_provider = FakeTreeProvider(trees={101: (), 102: ()})
    labels = FakeLabelProvider()

    class Sync(FakeSyncInstallationRepositories):
        async def execute(
            self,
            *,
            provider_installation_id: UUID,
            repositories: tuple[RepositoryOnboardingInput, ...],
        ) -> tuple[OnboardingResult, ...]:
            assert labels.calls == [
                (17, "octo/repository-101"),
                (17, "octo/repository-102"),
            ]
            return await super().execute(
                provider_installation_id=provider_installation_id, repositories=repositories
            )

    sync = Sync()
    asyncio.run(
        InstallationEventProjector(
            tree_provider=tree_provider, label_provider=labels, sync=sync
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(17, action, (first, second), ()),
        )
    )

    assert tree_provider.calls == [(17, 101, "main"), (17, 102, "main")]
    assert labels.calls == [(17, "octo/repository-101"), (17, "octo/repository-102")]
    assert len(sync.calls) == 1


def test_label_failure_is_logged_but_other_repositories_still_onboard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _snapshot(external_id=101)
    second = _snapshot(external_id=102)
    labels = FakeLabelProvider(fail_for={first.full_name})
    sync = FakeSyncInstallationRepositories()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=FakeTreeProvider(trees={101: (), 102: ()}),
            label_provider=labels,
            sync=sync,
        ).execute(
            provider_installation_id=uuid4(),
            event=InstallationRepositoriesEvent(17, "added", (first, second), ()),
        )
    )

    assert labels.calls == [(17, first.full_name), (17, second.full_name)]
    assert len(sync.calls) == 1
    assert [item.snapshot.full_name for item in sync.calls[0][1]] == [
        first.full_name,
        second.full_name,
    ]
    assert "GitHub label API unavailable" in caplog.text


def test_projector_fetches_each_declared_default_branch_before_entering_sync() -> None:
    first = _snapshot(external_id=101, branch="main")
    second = _snapshot(external_id=102, branch="trunk")
    provider = FakeTreeProvider(
        trees={
            101: (RepositoryTreeBlob(path="api/main.py", size=3, entry_type="blob"),),
            102: (RepositoryTreeBlob(path="web/app.ts", size=6, entry_type="blob"),),
        }
    )
    sync = FakeSyncInstallationRepositories()
    provider_installation_id = uuid4()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=provider, label_provider=FakeLabelProvider(), sync=sync
        ).execute(
            provider_installation_id=provider_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action="added",
                added_repositories=(first, second),
                removed_repositories=(),
            ),
        )
    )

    assert provider.calls == [(17, 101, "main"), (17, 102, "trunk")]
    assert len(sync.calls) == 1
    sync_installation_id, inputs = sync.calls[0]
    assert sync_installation_id == provider_installation_id
    assert [(item.snapshot.external_id, dict(item.languages)) for item in inputs] == [
        (101, {"Python": 100}),
        (102, {"TypeScript": 100}),
    ]


@pytest.mark.parametrize("action", ["deleted", "removed"])
def test_projector_soft_disables_removed_repositories_without_a_vcs_request(action: str) -> None:
    provider = FakeTreeProvider()
    labels = FakeLabelProvider()
    sync = FakeSyncInstallationRepositories()
    provider_installation_id = uuid4()

    asyncio.run(
        InstallationEventProjector(
            tree_provider=provider, label_provider=labels, sync=sync
        ).execute(
            provider_installation_id=provider_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action=action,  # type: ignore[arg-type]
                added_repositories=(),
                removed_repositories=(_snapshot(),),
            ),
        )
    )

    assert provider.calls == []
    assert labels.calls == []
    assert sync.calls == []
    assert sync.disable_calls == [(provider_installation_id, (_snapshot(),))]


def test_provider_failure_is_propagated_before_the_database_sync_begins() -> None:
    provider = FakeTreeProvider(error=RuntimeError("GitHub unavailable"))
    sync = FakeSyncInstallationRepositories()

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        asyncio.run(
            InstallationEventProjector(
                tree_provider=provider, label_provider=FakeLabelProvider(), sync=sync
            ).execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    installation_external_id=17,
                    action="created",
                    added_repositories=(_snapshot(),),
                    removed_repositories=(),
                ),
            )
        )

    assert provider.calls == [(17, 101, "main")]
    assert sync.calls == []
