"""Flush-only same-account archive selection at the fresh OAuth apply boundary."""

from __future__ import annotations

import logging
from collections import Counter
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.enums import RunState
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.run_notifications import notify_run_state
from app.modules.workspaces.application.link_github_installations import GitHubInstallation

_LOGGER = logging.getLogger(__name__)


class SqlAlchemyGitHubRepositoryRebinding:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def archived_workspace(self, snapshot: GitHubInstallation) -> UUID | None:
        # A persisted target account is not a substitute for THIS OAuth snapshot.
        owners = [ProviderInstallation.external_id == snapshot.id]
        if snapshot.account_external_id is not None:
            owners.append(ProviderInstallation.account_external_id == snapshot.account_external_id)
        installations = tuple(
            (
                await self._session.scalars(
                    select(ProviderInstallation)
                    .where(ProviderInstallation.provider == "github", or_(*owners))
                    .order_by(ProviderInstallation.external_id)
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if snapshot.account_external_id is None:
            return None
        sources = [
            item
            for item in installations
            if item.external_id != snapshot.id
            and item.account_external_id == snapshot.account_external_id
            and item.deleted_at is not None
        ]
        if not sources:
            return None
        repositories = tuple(
            (
                await self._session.scalars(
                    select(Repository).where(
                        Repository.provider_installation_id.in_([s.id for s in sources])
                    )
                )
            ).all()
        )
        workspaces = {source.workspace_id for source in sources}
        matches = Counter(
            repository.external_id
            for repository in repositories
            if repository.external_id in snapshot.repository_ids
        )
        if len(workspaces) != 1 or any(count > 1 for count in matches.values()):
            if workspaces:
                _LOGGER.info("GitHub archive lineage is ambiguous; retaining clean target identity")
            return None
        return next(iter(workspaces))

    async def rebind(self, target: ProviderInstallation, snapshot: GitHubInstallation) -> None:
        if target.deleted_at is not None or not snapshot.repository_ids:
            return
        workspace = await self.archived_workspace(snapshot)
        if workspace != target.workspace_id:
            return
        sources = select(ProviderInstallation.id).where(
            ProviderInstallation.provider == "github",
            ProviderInstallation.external_id != snapshot.id,
            ProviderInstallation.account_external_id == snapshot.account_external_id,
            ProviderInstallation.deleted_at.is_not(None),
            ProviderInstallation.workspace_id == workspace,
        )
        repositories = tuple(
            (
                await self._session.scalars(
                    select(Repository)
                    .where(
                        Repository.provider_installation_id.in_(sources),
                        Repository.external_id.in_(snapshot.repository_ids),
                    )
                    .order_by(Repository.external_id, Repository.id)
                    .with_for_update()
                )
            ).all()
        )
        target_ids = set(
            (
                await self._session.scalars(
                    select(Repository.external_id).where(
                        Repository.provider_installation_id == target.id
                    )
                )
            ).all()
        )
        for repository in repositories:
            if repository.external_id in target_ids:
                # Never replace an already-onboarded target or merge its children.
                continue
            # NULL legacy histories must be pinned while the original FK is still known.
            # Never replace an already recorded origin or rewrite terminal history payloads.
            await self._session.execute(
                update(Run)
                .where(
                    Run.origin_provider_installation_id.is_(None),
                    Run.code_change_id.in_(
                        select(CodeChange.id).where(CodeChange.repository_id == repository.id)
                    ),
                )
                .values(
                    origin_provider_installation_id=repository.provider_installation_id,
                    updated_at=Run.updated_at,
                )
            )
            # The immutable source, Repository and now Run locks stay held through the FK move.
            # A late worker/publisher finalization only accepts its original nonterminal state.
            cancelled = (
                await self._session.scalars(
                    update(Run)
                    .where(
                        Run.origin_provider_installation_id == repository.provider_installation_id,
                        Run.code_change_id.in_(
                            select(CodeChange.id).where(CodeChange.repository_id == repository.id)
                        ),
                        Run.state.in_([RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING]),
                    )
                    .values(
                        state=RunState.CANCELLED,
                        error_code="installation_replaced",
                        finished_at=func.now(),
                        lease_until=None,
                    )
                    .returning(Run.id)
                )
            ).all()
            for run_id in cancelled:
                await notify_run_state(self._session, run_id, RunState.CANCELLED)
            await self._session.execute(
                update(Repository)
                .where(Repository.id == repository.id)
                .values(
                    provider_installation_id=target.id,
                    installation_available=True,
                )
            )
        await self._session.flush()
