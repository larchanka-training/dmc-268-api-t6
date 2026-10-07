"""PostgreSQL receipt store and explicit transaction boundary."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.integrations.webhooks.infrastructure.models import (
    GitHubInstallationRemovalEffect,
    WebhookEvent,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation


class SqlAlchemyGitHubWebhookReceiptStore:
    """Session-bound receipt operations; commits belong to the use case."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, delivery: WebhookReceipt) -> bool:
        payload = json.loads(delivery.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("GitHub receipt JSON must be an object")
        installation = payload.get("installation")
        installation_id = installation.get("id") if isinstance(installation, dict) else None
        action = payload.get("action")
        statement = (
            insert(WebhookEvent)
            .values(
                delivery_id=delivery.delivery_id,
                event=delivery.event_name,
                action=action if isinstance(action, str) else None,
                installation_external_id=(
                    installation_id if isinstance(installation_id, int) else None
                ),
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=[WebhookEvent.delivery_id])
            .returning(WebhookEvent.id)
        )
        return await self._session.scalar(statement) is not None

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> WebhookReceipt | None:
        statement = (
            update(WebhookEvent)
            .where(
                WebhookEvent.delivery_id == delivery_id,
                WebhookEvent.payload.is_not(None),
                WebhookEvent.projected_at.is_(None),
                WebhookEvent.projection_failed_at.is_(None),
                WebhookEvent.projection_deferred_at.is_(None),
                or_(
                    WebhookEvent.projection_lease_until.is_(None),
                    WebhookEvent.projection_lease_until <= now,
                ),
                or_(WebhookEvent.retry_after.is_(None), WebhookEvent.retry_after <= now),
            )
            .values(projection_claim_token=token, projection_lease_until=until)
            .returning(WebhookEvent.event, WebhookEvent.payload)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        return WebhookReceipt(
            delivery_id=delivery_id,
            event_name=cast(str, row[0]),
            payload_json=json.dumps(row[1]),
        )

    async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
        statement = (
            update(WebhookEvent)
            .where(
                WebhookEvent.delivery_id == delivery_id,
                WebhookEvent.projection_claim_token == token,
                WebhookEvent.projected_at.is_(None),
            )
            .values(
                projected_at=at,
                projection_claim_token=None,
                projection_lease_until=None,
                retry_after=None,
            )
            .returning(WebhookEvent.id)
        )
        result = await self._session.execute(statement)
        if result.scalar_one_or_none() is None:
            raise RuntimeError("webhook projection claim was lost")

    async def release(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        deferred_at: datetime,
        max_attempts: int,
    ) -> bool:
        next_attempt_count = WebhookEvent.projection_attempt_count + 1
        is_final_attempt = next_attempt_count >= max_attempts
        statement = (
            update(WebhookEvent)
            .where(
                WebhookEvent.delivery_id == delivery_id,
                WebhookEvent.projection_claim_token == token,
                WebhookEvent.projected_at.is_(None),
            )
            .values(
                projection_claim_token=None,
                projection_lease_until=None,
                projection_attempt_count=next_attempt_count,
                projection_deferred_at=case((is_final_attempt, deferred_at), else_=None),
                retry_after=case((is_final_attempt, None), else_=retry_after),
            )
            .returning(WebhookEvent.projection_deferred_at)
        )
        return await self._session.scalar(statement) is not None

    async def purge_finished(self, before: datetime) -> int:
        statement = (
            delete(WebhookEvent)
            .where(
                or_(
                    WebhookEvent.projected_at < before,
                    WebhookEvent.projection_failed_at < before,
                    WebhookEvent.projection_deferred_at < before,
                )
            )
            .execution_options(synchronize_session=False)
        )
        # rowcount, not RETURNING: a large purge would load every deleted id into Python.
        # Turning the session sync off is defensive here: "auto" evaluates these comparisons
        # without RETURNING, but a fallback to "fetch" would add it back.
        result = cast(CursorResult[Any], await self._session.execute(statement))
        # Keep markers while their receipt can still be replayed. Delete receipts first
        # so expired finished deliveries and their markers leave in the same transaction.
        matching_receipt = select(WebhookEvent.id).where(
            WebhookEvent.delivery_id == GitHubInstallationRemovalEffect.delivery_id
        )
        await self._session.execute(
            delete(GitHubInstallationRemovalEffect)
            .where(
                GitHubInstallationRemovalEffect.created_at < before,
                ~matching_receipt.exists(),
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount

    async def revive_deferred_installation_deliveries(
        self, *, deferred_before: datetime, received_after: datetime
    ) -> int:
        linked_installation = select(ProviderInstallation.id).where(
            ProviderInstallation.provider == "github",
            ProviderInstallation.external_id == WebhookEvent.installation_external_id,
        )
        statement = (
            update(WebhookEvent)
            .where(
                WebhookEvent.event.in_(("installation", "installation_repositories")),
                WebhookEvent.payload.is_not(None),
                WebhookEvent.projected_at.is_(None),
                WebhookEvent.projection_failed_at.is_(None),
                WebhookEvent.projection_deferred_at.is_not(None),
                WebhookEvent.projection_deferred_at <= deferred_before,
                WebhookEvent.received_at >= received_after,
                linked_installation.exists(),
            )
            .values(projection_deferred_at=None, retry_after=None, projection_attempt_count=0)
            .execution_options(synchronize_session=False)
        )
        # rowcount, not RETURNING: only the count is used. Turning the session sync off is
        # required here: with the EXISTS subquery "auto" falls back to "fetch", which adds
        # RETURNING back.
        result = cast(CursorResult[Any], await self._session.execute(statement))
        return result.rowcount

    async def release_after_dispatch_failure(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        failed_at: datetime,
        max_attempts: int,
    ) -> bool:
        next_attempt_count = WebhookEvent.projection_attempt_count + 1
        is_final_attempt = next_attempt_count >= max_attempts
        statement = (
            update(WebhookEvent)
            .where(
                WebhookEvent.delivery_id == delivery_id,
                WebhookEvent.projection_claim_token == token,
                WebhookEvent.projected_at.is_(None),
                WebhookEvent.projection_failed_at.is_(None),
            )
            .values(
                projection_claim_token=None,
                projection_lease_until=None,
                projection_attempt_count=next_attempt_count,
                projection_failed_at=case((is_final_attempt, failed_at), else_=None),
                retry_after=case((is_final_attempt, None), else_=retry_after),
            )
            .returning(WebhookEvent.projection_failed_at)
        )
        return await self._session.scalar(statement) is not None

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        statement = (
            select(WebhookEvent.delivery_id)
            .where(
                WebhookEvent.payload.is_not(None),
                WebhookEvent.projected_at.is_(None),
                WebhookEvent.projection_failed_at.is_(None),
                WebhookEvent.projection_deferred_at.is_(None),
                or_(
                    WebhookEvent.projection_lease_until.is_(None),
                    WebhookEvent.projection_lease_until <= now,
                ),
                or_(WebhookEvent.retry_after.is_(None), WebhookEvent.retry_after <= now),
            )
            .order_by(WebhookEvent.received_at, WebhookEvent.id)
            .limit(limit)
        )
        return tuple((await self._session.scalars(statement)).all())


class SqlAlchemyGitHubWebhookReceiptUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def receipts(self) -> SqlAlchemyGitHubWebhookReceiptStore:
        return SqlAlchemyGitHubWebhookReceiptStore(self.session)
