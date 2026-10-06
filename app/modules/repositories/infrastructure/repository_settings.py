"""PostgreSQL adapter for repository review settings, scoped by the portal claim."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import Engine, ReviewEvent, WaitForCi
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.application.repository_settings import (
    RepositorySettings,
    RepositorySettingsChange,
)
from app.modules.repositories.infrastructure.models import Repository
from app.modules.workspaces.infrastructure.repository_access import repository_access_predicate


def _to_settings(repository: Repository) -> RepositorySettings:
    return RepositorySettings(
        id=repository.id,
        full_name=repository.full_name,
        url=repository.web_url,
        default_branch=repository.default_branch,
        enabled=repository.enabled,
        default_engine=repository.default_engine.value,
        wait_for_ci=repository.wait_for_ci.value,
        max_comments=repository.max_comments,
        # The API spells GitHub's review event; the PG enum stores it in lower case.
        review_event=repository.review_event.value.upper(),
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
        self._allow_unscoped = allow_unscoped

    def _visible(self) -> ColumnElement[bool]:
        if self._scope is None:
            if not self._allow_unscoped:
                raise ValueError("SqlAlchemyRepositorySettingsStore requires an AuthScope")
            return true()
        return repository_access_predicate(self._scope)

    async def list_repositories(self) -> list[RepositorySettings]:
        rows = await self._session.scalars(
            select(Repository).where(self._visible()).order_by(Repository.full_name, Repository.id)
        )
        return [_to_settings(row) for row in rows]

    async def get_repository(self, repository_id: UUID) -> RepositorySettings | None:
        row = await self._session.scalar(
            select(Repository).where(Repository.id == repository_id, self._visible())
        )
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
        row = await self._session.scalar(
            update(Repository)
            .where(Repository.id == repository_id, self._visible())
            .values(**values)
            .returning(Repository)
        )
        return None if row is None else _to_settings(row)


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
