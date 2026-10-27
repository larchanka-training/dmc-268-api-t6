"""PostgreSQL adapters of the repository rules status (#150).

The portal store is scoped by the claim like the repository settings; the worker store
sees every repository. A request is the ``refresh_requested_at`` marker of the row: it
is written in the requesting transaction together with a ``webhook_work_available``
hint, so a committed request is never lost, and a lost hint only waits for the next
sweep of ``webhook-worker``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, null, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.webhooks.infrastructure.webhook_notifications import (
    notify_webhook_work,
)
from app.modules.repositories.application.rules_status import (
    RulesDiagnostic,
    RulesReading,
    RulesRefreshTarget,
    RulesStatus,
    RulesStatusKind,
    StoredRulesStatus,
)
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RepositoryRulesStatus,
    RuleVersion,
)
from app.modules.workspaces.infrastructure.repository_access import repository_access_predicate

_LOGGER = logging.getLogger(__name__)

# Shown for a missing document when the repository has no active rule version, which
# onboarding always creates; the row constraint needs a value.
_UNKNOWN_DEFAULTS_VERSION = "unknown"


async def request_rules_refresh(session: AsyncSession, repository_id: UUID) -> None:
    """Mark the repository for a new read and wake ``webhook-worker`` on commit.

    An older pending request is kept while no worker holds it: the read that answers it is
    still to come. A claimed request gets a new time instead, so ``save``, which clears
    only the claimed time, leaves it for the next read: the running read may have resolved
    the branch head before the change that caused this request.
    """
    await session.execute(
        insert(RepositoryRulesStatus)
        .values(repository_id=repository_id, refresh_requested_at=func.clock_timestamp())
        .on_conflict_do_update(
            index_elements=[RepositoryRulesStatus.repository_id],
            set_={
                "refresh_requested_at": case(
                    (
                        RepositoryRulesStatus.refresh_claimed_until.is_not(None),
                        func.clock_timestamp(),
                    ),
                    else_=func.coalesce(
                        RepositoryRulesStatus.refresh_requested_at, func.clock_timestamp()
                    ),
                )
            },
        )
    )
    await notify_webhook_work(session)


def _diagnostic(item: dict[str, Any]) -> RulesDiagnostic:
    return RulesDiagnostic(
        code=item["code"],
        message=item["message"],
        line=item.get("line"),
        column=item.get("column"),
        retryable=item["retryable"],
        limit=item.get("limit"),
        actual=item.get("actual"),
        # Rows stored before the flag have no key; no read reported a lower bound then.
        actual_is_lower_bound=item.get("actual_is_lower_bound", False),
        pattern_index=item.get("pattern_index"),
        retry_after=(
            None
            if item.get("retry_after_seconds") is None
            else timedelta(seconds=item["retry_after_seconds"])
        ),
    )


def _diagnostic_json(item: RulesDiagnostic) -> dict[str, Any]:
    return {
        "code": item.code,
        "message": item.message,
        "line": item.line,
        "column": item.column,
        "retryable": item.retryable,
        "limit": item.limit,
        "actual": item.actual,
        "actual_is_lower_bound": item.actual_is_lower_bound,
        "pattern_index": item.pattern_index,
        "retry_after_seconds": (
            None if item.retry_after is None else int(item.retry_after.total_seconds())
        ),
    }


def _to_status(row: RepositoryRulesStatus) -> RulesStatus | None:
    if row.status is None or row.branch is None:
        return None
    return RulesStatus(
        status=RulesStatusKind(row.status),
        branch=row.branch,
        sha=row.sha,
        checked_at=row.checked_at,
        text=row.content,
        checksum=row.checksum,
        ignore_masks=None if row.ignore_masks is None else tuple(row.ignore_masks),
        diagnostics=tuple(_diagnostic(item) for item in row.diagnostics),
        defaults_version=row.defaults_version,
    )


class SqlAlchemyRulesStatusStore:
    def __init__(
        self, session: AsyncSession, scope: AuthScope | None, *, allow_unscoped: bool = False
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyRulesStatusStore requires an AuthScope unless allow_unscoped=True"
            )
        self._session = session
        self._scope = scope

    def _visible(self) -> ColumnElement[bool]:
        return true() if self._scope is None else repository_access_predicate(self._scope)

    async def get(self, repository_id: UUID) -> StoredRulesStatus | None:
        row = (
            await self._session.execute(
                select(
                    Repository.default_branch,
                    Repository.installation_available,
                    RepositoryRulesStatus,
                )
                .outerjoin(
                    RepositoryRulesStatus, RepositoryRulesStatus.repository_id == Repository.id
                )
                .where(Repository.id == repository_id, self._visible())
            )
        ).one_or_none()
        if row is None:
            return None
        stored: RepositoryRulesStatus | None = row.RepositoryRulesStatus
        return StoredRulesStatus(
            default_branch=row.default_branch,
            status=None if stored is None else _to_status(stored),
            refresh_requested=stored is not None and stored.refresh_requested_at is not None,
            installation_available=row.installation_available,
        )

    async def request_refresh(self, repository_id: UUID) -> None:
        await request_rules_refresh(self._session, repository_id)


class SqlAlchemyRulesStatusUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyRulesStatusUnitOfWork requires an AuthScope unless allow_unscoped=True"
            )
        super().__init__(session_factory)
        self._scope = scope
        self._allow_unscoped = allow_unscoped

    @property
    def rules_status(self) -> SqlAlchemyRulesStatusStore:
        return SqlAlchemyRulesStatusStore(
            self.session, self._scope, allow_unscoped=self._allow_unscoped
        )


class SqlAlchemyRulesRefreshStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, *, now: datetime, until: datetime, limit: int
    ) -> tuple[RulesRefreshTarget, ...]:
        # Only a repository the App can still read: a removed one keeps its request until
        # it is connected again, and onboarding then requests a read anyway.
        rows = (
            await self._session.execute(
                select(
                    RepositoryRulesStatus.repository_id,
                    RepositoryRulesStatus.refresh_requested_at,
                    ProviderInstallation.external_id,
                    Repository.full_name,
                    Repository.default_branch,
                )
                .join(Repository, Repository.id == RepositoryRulesStatus.repository_id)
                .join(
                    ProviderInstallation,
                    ProviderInstallation.id == Repository.provider_installation_id,
                )
                .where(
                    RepositoryRulesStatus.refresh_requested_at.is_not(None),
                    or_(
                        RepositoryRulesStatus.refresh_claimed_until.is_(None),
                        RepositoryRulesStatus.refresh_claimed_until <= now,
                    ),
                    Repository.installation_available,
                    ProviderInstallation.provider == "github",
                    ProviderInstallation.deleted_at.is_(None),
                )
                .order_by(RepositoryRulesStatus.refresh_requested_at)
                .limit(limit)
                .with_for_update(of=RepositoryRulesStatus, skip_locked=True)
            )
        ).all()
        if not rows:
            return ()
        await self._session.execute(
            update(RepositoryRulesStatus)
            .where(RepositoryRulesStatus.repository_id.in_([row.repository_id for row in rows]))
            .values(refresh_claimed_until=until)
        )
        return tuple(
            RulesRefreshTarget(
                repository_id=row.repository_id,
                installation_external_id=row.external_id,
                repository_full_name=row.full_name,
                default_branch=row.default_branch,
                requested_at=row.refresh_requested_at,
                claimed_until=until,
            )
            for row in rows
        )

    async def save(
        self, target: RulesRefreshTarget, reading: RulesReading, *, checked_at: datetime
    ) -> bool:
        defaults_version: str | None = None
        if reading.status is RulesStatusKind.MISSING:
            version = await self._session.scalar(
                select(RuleVersion.version).where(
                    RuleVersion.repository_id == target.repository_id, RuleVersion.is_active
                )
            )
            if version is None:
                # Onboarding always creates one; shown as "unknown" rather than failing.
                _LOGGER.warning(
                    "Repository %s has no active rule version; defaultsVersion is unknown",
                    target.repository_id,
                )
            defaults_version = _UNKNOWN_DEFAULTS_VERSION if version is None else f"v{version}"
        saved = await self._session.scalar(
            update(RepositoryRulesStatus)
            .where(
                RepositoryRulesStatus.repository_id == target.repository_id,
                # Still our claim: after the lease another worker may hold a newer read.
                RepositoryRulesStatus.refresh_claimed_until == target.claimed_until,
            )
            .values(
                status=reading.status.value,
                branch=reading.branch,
                sha=None if reading.sha is None else reading.sha.lower(),
                content=reading.text,
                checksum=reading.checksum,
                ignore_masks=None if reading.ignore_masks is None else list(reading.ignore_masks),
                diagnostics=[_diagnostic_json(item) for item in reading.diagnostics],
                defaults_version=defaults_version,
                checked_at=checked_at,
                refresh_requested_at=case(
                    (
                        RepositoryRulesStatus.refresh_requested_at == target.requested_at,
                        null(),
                    ),
                    else_=RepositoryRulesStatus.refresh_requested_at,
                ),
                refresh_claimed_until=None,
            )
            .returning(RepositoryRulesStatus.repository_id)
        )
        return saved is not None

    async def request_for_provider_repository(
        self, installation_external_id: int, repository_external_id: int
    ) -> bool:
        repository_id = await self._session.scalar(
            select(Repository.id)
            .join(ProviderInstallation)
            .where(
                ProviderInstallation.provider == "github",
                ProviderInstallation.external_id == installation_external_id,
                ProviderInstallation.deleted_at.is_(None),
                Repository.external_id == repository_external_id,
                Repository.installation_available,
            )
        )
        if repository_id is None:
            return False
        await request_rules_refresh(self._session, repository_id)
        return True


class SqlAlchemyRulesRefreshUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def rules(self) -> SqlAlchemyRulesRefreshStore:
        return SqlAlchemyRulesRefreshStore(self.session)
