"""PostgreSQL receipt store and explicit transaction boundary."""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent


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

    async def release(self, delivery_id: str, token: UUID, retry_after: datetime) -> None:
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
                retry_after=retry_after,
            )
        )
        await self._session.execute(statement)

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        statement = (
            select(WebhookEvent.delivery_id)
            .where(
                WebhookEvent.payload.is_not(None),
                WebhookEvent.projected_at.is_(None),
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
