"""Callable composition for repository onboarding from installation events.

This module deliberately stops at the application boundary.  A delivery
consumer supplies a validated :class:`InstallationRepositoriesEvent` and the
internal ``ProviderInstallation.id`` that it resolved before calling this
handler.  HTTP verification, queueing, and GitHub token management are owned
by their respective entrypoints rather than this composition root.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationEventProjector,
    InstallationRepositoryDetailsProvider,
    InstallationRepositoryLabelProvider,
    InstallationRepositoryTreeProvider,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
)
from app.modules.repositories.application.onboard_repository import (
    OnboardingResult,
    load_default_rule_sets,
)
from app.modules.repositories.application.sync_installation_repositories import (
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)

_DEFAULT_RULES_DIR = Path(__file__).resolve().parents[2] / "review" / "rules"


class InstallationOnboarding:
    """Compose the existing tree, language and transactional onboarding flow.

    Rule assets are parsed once when the long-lived handler is created.  Tree
    and repository-details I/O stay in ``InstallationEventProjector`` and
    therefore always finish before ``SyncInstallationRepositories`` opens its
    database transaction.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        tree_provider: InstallationRepositoryTreeProvider,
        label_provider: InstallationRepositoryLabelProvider,
        details_provider: InstallationRepositoryDetailsProvider,
        rules_dir: Path | None = None,
    ) -> None:
        rule_sets = load_default_rule_sets(rules_dir or _DEFAULT_RULES_DIR)
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(session_factory),
            rule_sets=rule_sets,
        )
        self._projector = InstallationEventProjector(
            tree_provider=tree_provider,
            label_provider=label_provider,
            details_provider=details_provider,
            sync=sync,
        )

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        event: InstallationRepositoriesEvent,
    ) -> tuple[OnboardingResult, ...]:
        """Synchronize one already-validated installation repository event."""
        return await self._projector.execute(
            provider_installation_id=provider_installation_id,
            event=event,
        )
