"""Behavioural contract for the installation-event application composition."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.bootstrap.installation_onboarding import InstallationOnboarding
from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationRepositoryTreeProvider,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositorySnapshot,
    RepositoryTreeBlob,
)
from app.modules.repositories.application.onboard_repository import (
    DefaultRuleSet,
    OnboardingResult,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)


@dataclass
class FakeTreeProvider(InstallationRepositoryTreeProvider):
    calls: list[tuple[int, int]] = field(default_factory=list)

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        self.calls.append((installation_external_id, repository.external_id))
        return (RepositoryTreeBlob(path="src/app.ts", size=10, entry_type="blob"),)


def test_composition_accepts_typed_event_and_internal_installation_id(
    monkeypatch: Any,
) -> None:
    """The callable boundary sends a typed event through the composed projector."""
    provider = FakeTreeProvider()
    internal_installation_id = uuid4()
    observed: dict[str, object] = {}

    class FakeSync:
        def __init__(self, *, uow_factory: object, rule_sets: object) -> None:
            observed["rule_sets"] = rule_sets

        async def execute(
            self, *, provider_installation_id: UUID, repositories: object
        ) -> tuple[OnboardingResult, ...]:
            observed["provider_installation_id"] = provider_installation_id
            observed["repositories"] = repositories
            return ()

        async def disable(self, *, provider_installation_id: UUID, repositories: object) -> None:
            raise AssertionError("added event must not disable repositories")

    monkeypatch.setattr(
        "app.bootstrap.installation_onboarding.SyncInstallationRepositories", FakeSync
    )
    handler = InstallationOnboarding(
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        tree_provider=provider,
        rules_dir=Path("review/rules"),
    )

    asyncio.run(
        handler.execute(
            provider_installation_id=internal_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action="added",
                added_repositories=(
                    RepositorySnapshot(
                        id=101,
                        full_name="octo/web",
                        default_branch="main",
                        html_url="https://github.com/octo/web",
                    ),
                ),
                removed_repositories=(),
            ),
        )
    )

    assert provider.calls == [(17, 101)]
    assert observed["provider_installation_id"] == internal_installation_id
    repositories = cast(tuple[RepositoryOnboardingInput, ...], observed["repositories"])
    rule_sets = cast(Mapping[str, DefaultRuleSet], observed["rule_sets"])
    assert repositories[0].languages == {"TypeScript": 100}
    assert set(rule_sets) == {"backend", "frontend"}


def test_resources_composes_onboarding_with_cwd_independent_default_rules(tmp_path: Path) -> None:
    """The running app's resource root exposes the real onboarding composition."""
    previous_directory = Path.cwd()
    os.chdir(tmp_path)
    try:
        provider = FakeTreeProvider()
        resources = ReviewsApiResources(
            cast(AsyncEngine, object()),
            cast(async_sessionmaker[AsyncSession], object()),
        )

        handler = resources.installation_onboarding(provider)

        result = asyncio.run(
            handler.execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    installation_external_id=17,
                    action="created",
                    added_repositories=(),
                    removed_repositories=(),
                ),
            )
        )
    finally:
        os.chdir(previous_directory)

    assert result == ()
    assert provider.calls == []
