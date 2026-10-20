"""PostgreSQL adapter for repository review settings, scoped by the portal claim."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Row, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import Engine, ReviewEvent, SeverityThreshold, WaitForCi
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.scope import AuthScope
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.domain.rag_index import rag_index
from app.modules.code_index.infrastructure.effective_rag import GITHUB_PROVIDER
from app.modules.code_index.infrastructure.models import CodeIndexState
from app.modules.code_index.infrastructure.purger import (
    SqlAlchemyCodeIndexEnabler,
    SqlAlchemyCodeIndexPurger,
)
from app.modules.repositories.application.repository_settings import (
    RepositorySettings,
    RepositorySettingsChange,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.repository_access import repository_access_predicate

# The repository row and the state columns the RAG index status is derived from (#145); the
# outer join gives None for each of them when the repository has no state row yet.
# `_to_settings` reads them by name, so the column order here carries no meaning.
_SETTINGS = select(
    Repository,
    CodeIndexState.status,
    CodeIndexState.indexed_commit_sha,
    CodeIndexState.target_commit_sha,
    CodeIndexState.published_at,
    CodeIndexState.file_count,
    CodeIndexState.symbol_count,
    CodeIndexState.error_code,
    CodeIndexState.last_error_code,
).outerjoin(CodeIndexState, CodeIndexState.repository_id == Repository.id)


def _visible(scope: AuthScope | None) -> ColumnElement[bool]:
    return true() if scope is None else repository_access_predicate(scope)


def _to_settings(row: Row[Any]) -> RepositorySettings:
    repository: Repository = row.Repository
    rag_enabled = repository.rag_enabled and repository.installation_available
    return RepositorySettings(
        id=repository.id,
        full_name=repository.full_name,
        url=repository.web_url,
        default_branch=repository.default_branch,
        enabled=repository.enabled and repository.installation_available,
        default_engine=repository.default_engine.value,
        wait_for_ci=repository.wait_for_ci.value,
        max_comments=repository.max_comments,
        # The API spells GitHub's review event; the PG enum stores it in lower case.
        review_event=repository.review_event.value.upper(),
        branch_filter=tuple(repository.branch_filter),
        severity_threshold=repository.severity_threshold.value,
        installation_available=repository.installation_available,
        rag_enabled=rag_enabled,
        rag_index=rag_index(
            rag_enabled=rag_enabled,
            status=row.status,
            indexed_commit_sha=row.indexed_commit_sha,
            target_commit_sha=row.target_commit_sha,
            published_at=row.published_at,
            file_count=row.file_count,
            symbol_count=row.symbol_count,
            error_code=row.error_code,
            last_error_code=row.last_error_code,
        ),
    )


class SqlAlchemyRepositorySettingsStore:
    def __init__(
        self,
        session: AsyncSession,
        scope: AuthScope | None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyRepositorySettingsStore requires an AuthScope unless allow_unscoped=True"
            )
        self._session = session
        self._scope = scope

    def _visible(self) -> ColumnElement[bool]:
        return _visible(self._scope)

    async def list_repositories(self) -> list[RepositorySettings]:
        rows = await self._session.execute(
            _SETTINGS.where(self._visible()).order_by(Repository.full_name, Repository.id)
        )
        return [_to_settings(row) for row in rows]

    async def get_repository(self, repository_id: UUID) -> RepositorySettings | None:
        row = (
            await self._session.execute(
                _SETTINGS.where(Repository.id == repository_id, self._visible())
            )
        ).one_or_none()
        return None if row is None else _to_settings(row)

    async def update_repository(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None:
        values: dict[str, object] = {}
        if change.enabled is not None:
            values["enabled"] = change.enabled
        if change.default_engine is not None:
            values["default_engine"] = Engine(change.default_engine)
        if change.wait_for_ci is not None:
            values["wait_for_ci"] = WaitForCi(change.wait_for_ci)
        if change.max_comments is not None:
            values["max_comments"] = change.max_comments
        if change.review_event is not None:
            values["review_event"] = ReviewEvent(change.review_event.lower())
        if change.branch_filter is not None:
            values["branch_filter"] = list(change.branch_filter)
        if change.severity_threshold is not None:
            values["severity_threshold"] = SeverityThreshold(change.severity_threshold)
        if change.rag_enabled is not None:
            # The stored preference; the read below masks it by the installation, like enabled.
            values["rag_enabled"] = change.rag_enabled
        updated = await self._session.scalar(
            update(Repository)
            .where(Repository.id == repository_id, self._visible())
            .values(**values)
            .returning(Repository.id)
        )
        return None if updated is None else await self.get_repository(updated)


class SqlAlchemyScopedCodeIndexPurger:
    """`CodeIndexPurger` over the repositories the scope sees, on the caller's session.

    Purging a repository outside the caller's Workspaces is a no-op, like an update of
    one; the purge itself is `SqlAlchemyCodeIndexPurger`'s. Never commits.
    """

    def __init__(
        self,
        session: AsyncSession,
        scope: AuthScope | None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyScopedCodeIndexPurger requires an AuthScope unless allow_unscoped=True"
            )
        self._session = session
        self._scope = scope
        self._purger = SqlAlchemyCodeIndexPurger(session)

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        visible = await self._session.scalar(
            select(Repository.id).where(Repository.id == repository_id, _visible(self._scope))
        )
        if visible is not None:
            await self._purger.purge(repository_id, reason)


class SqlAlchemyScopedCodeIndexEnabler:
    """`CodeIndexEnabler` over the repositories the scope sees, on the caller's session.

    Enabling the index of a repository outside the caller's Workspaces is a no-op, like
    an update of one, and so is one of another provider than GitHub, which the leader's
    re-arm never lists either; the enable itself is `SqlAlchemyCodeIndexEnabler`'s.
    Never commits.
    """

    def __init__(
        self,
        session: AsyncSession,
        scope: AuthScope | None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyScopedCodeIndexEnabler requires an AuthScope unless allow_unscoped=True"
            )
        self._session = session
        self._scope = scope
        self._enabler = SqlAlchemyCodeIndexEnabler(session)

    async def enable(self, repository_id: UUID) -> None:
        visible = await self._session.scalar(
            select(Repository.id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .where(Repository.id == repository_id, GITHUB_PROVIDER, _visible(self._scope))
        )
        if visible is not None:
            await self._enabler.enable(repository_id)


class SqlAlchemyRepositorySettingsUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyRepositorySettingsUnitOfWork requires an AuthScope "
                "unless allow_unscoped=True"
            )
        super().__init__(session_factory)
        self._scope = scope
        self._allow_unscoped = allow_unscoped

    @property
    def repositories(self) -> SqlAlchemyRepositorySettingsStore:
        return SqlAlchemyRepositorySettingsStore(
            self.session, self._scope, allow_unscoped=self._allow_unscoped
        )

    @property
    def index_purger(self) -> SqlAlchemyScopedCodeIndexPurger:
        return SqlAlchemyScopedCodeIndexPurger(
            self.session, self._scope, allow_unscoped=self._allow_unscoped
        )

    @property
    def index_enabler(self) -> SqlAlchemyScopedCodeIndexEnabler:
        return SqlAlchemyScopedCodeIndexEnabler(
            self.session, self._scope, allow_unscoped=self._allow_unscoped
        )
