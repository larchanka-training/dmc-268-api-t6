"""SQLAlchemy adapter for creating a repository's initial active rule version."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.repositories.application.onboard_repository import PersistedRuleVersion
from app.modules.repositories.infrastructure.models import RuleVersion


class SqlAlchemyRepositoryRuleVersionStore:
    """Flush-only persistence adapter; the caller owns the transaction commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None:
        row = await self._session.scalar(
            select(RuleVersion).where(
                RuleVersion.repository_id == repository_id, RuleVersion.is_active.is_(True)
            )
        )
        return _to_persisted(row) if row is not None else None

    async def get_or_create_initial_rule_version(
        self, repository_id: UUID, version: int, rules: list[dict[str, object]]
    ) -> PersistedRuleVersion:
        row = await self._session.scalar(
            select(RuleVersion).where(
                RuleVersion.repository_id == repository_id, RuleVersion.is_active.is_(True)
            )
        )
        if row is None:
            row = RuleVersion.from_rules(
                repository_id=repository_id,
                version=version,
                rules=[dict(rule) for rule in rules],
            )
            self._session.add(row)
            await self._session.flush()
        return _to_persisted(row)


def _to_persisted(row: RuleVersion) -> PersistedRuleVersion:
    rules: list[dict[str, object]] = [dict(rule) for rule in row.rules]
    return PersistedRuleVersion(
        repository_id=row.repository_id,
        version=row.version,
        rules=rules,
        checksum=row.checksum,
    )
