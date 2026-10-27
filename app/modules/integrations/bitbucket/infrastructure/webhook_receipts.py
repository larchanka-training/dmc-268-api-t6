"""Bitbucket receipt admission under repository binding and secret fences."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Text, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.bitbucket.application.webhook_receipts import (
    BitbucketReceiptError,
    validate_bitbucket_receipt,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyWebhookReceiptOperations,
)
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent
from app.modules.integrations.webhooks.infrastructure.webhook_notifications import (
    notify_webhook_work,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.models import Workspace


class SqlAlchemyBitbucketWebhookReceiptStore(SqlAlchemyWebhookReceiptOperations):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, provider="bitbucket")

    async def _active_origin(self, receipt: WebhookReceipt) -> tuple[int, UUID] | None:
        installation_id = receipt.origin_provider_installation_id
        repository_id = receipt.origin_repository_id
        if installation_id is None or repository_id is None:
            return None
        workspace_id = await self._session.scalar(
            select(ProviderInstallation.workspace_id).where(
                ProviderInstallation.id == installation_id,
                ProviderInstallation.provider == "bitbucket",
            )
        )
        if workspace_id is None:
            return None
        # Match management's lock order. A concurrent rotation/disconnect either
        # commits first and changes the generation, or waits for this receipt commit.
        workspace = await self._session.scalar(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            return None
        parent = await self._session.scalar(
            select(ProviderInstallation)
            .where(
                ProviderInstallation.id == installation_id,
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.provider == "bitbucket",
            )
            .with_for_update()
        )
        if parent is None or parent.deleted_at is not None:
            return None
        connection = await self._session.scalar(
            select(BitbucketConnection)
            .where(
                BitbucketConnection.provider_installation_id == installation_id,
                BitbucketConnection.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if (
            connection is None
            or connection.revoked_at is not None
            or connection.token_ciphertext is None
        ):
            return None
        repository = await self._session.scalar(
            select(Repository)
            .where(
                Repository.id == repository_id,
                Repository.provider_installation_id == installation_id,
            )
            .with_for_update()
        )
        if repository is None or not repository.enabled or not repository.installation_available:
            return None
        identity = await self._session.scalar(
            select(BitbucketRepositoryIdentity).where(
                BitbucketRepositoryIdentity.repository_id == repository_id,
                BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                BitbucketRepositoryIdentity.workspace_id == workspace_id,
            )
        )
        webhook = await self._session.scalar(
            select(BitbucketRepositoryWebhookSecret)
            .where(BitbucketRepositoryWebhookSecret.repository_id == repository_id)
            .with_for_update()
        )
        if (
            identity is None
            or webhook is None
            or webhook.revoked_at is not None
            or webhook.secret_ciphertext is None
            or webhook.secret_key_id is None
            or webhook.binding_generation != receipt.origin_binding_generation
            or webhook.generation != receipt.origin_webhook_generation
        ):
            return None
        return parent.external_id, identity.repository_uuid

    async def save(self, delivery: WebhookReceipt) -> bool:
        origin = await self._active_origin(delivery)
        if origin is None:
            raise BitbucketReceiptError("inactive_origin")
        installation_external_id, repository_uuid = origin
        payload = validate_bitbucket_receipt(delivery, repository_uuid)
        attributes = payload.get("pullrequest")
        action = delivery.event_name.partition(":")[2]
        unsupported = not (
            delivery.event_name.startswith("pullrequest:") or delivery.event_name == "repo:push"
        )
        canonical_payload = cast(literal(delivery.payload_json), JSONB)
        inserted = await self._session.scalar(
            insert(WebhookEvent)
            .values(
                provider="bitbucket",
                provider_installation_id=delivery.origin_provider_installation_id,
                bitbucket_repository_id=delivery.origin_repository_id,
                bitbucket_binding_generation=delivery.origin_binding_generation,
                bitbucket_webhook_generation=delivery.origin_webhook_generation,
                installation_external_id=installation_external_id,
                delivery_id=delivery.delivery_id,
                event=delivery.event_name,
                action=action if isinstance(attributes, dict) else None,
                payload=canonical_payload,
                projected_at=func.now() if unsupported else None,
            )
            .on_conflict_do_nothing(index_elements=[WebhookEvent.delivery_id])
            .returning(WebhookEvent.id)
        )
        if inserted is None:
            identical = await self._session.scalar(
                select(WebhookEvent.id).where(
                    WebhookEvent.delivery_id == delivery.delivery_id,
                    WebhookEvent.provider == "bitbucket",
                    WebhookEvent.provider_installation_id
                    == delivery.origin_provider_installation_id,
                    WebhookEvent.bitbucket_repository_id == delivery.origin_repository_id,
                    WebhookEvent.bitbucket_binding_generation == delivery.origin_binding_generation,
                    WebhookEvent.bitbucket_webhook_generation == delivery.origin_webhook_generation,
                    WebhookEvent.event == delivery.event_name,
                    WebhookEvent.payload == canonical_payload,
                )
            )
            if identical is None:
                raise BitbucketReceiptError("delivery_conflict")
            return False
        # First authenticated signed delivery proves this repository's configured
        # hook reached us. This shares the receipt's commit boundary.
        connection = await self._session.scalar(
            select(BitbucketConnection)
            .where(
                BitbucketConnection.provider_installation_id
                == delivery.origin_provider_installation_id
            )
            .with_for_update()
        )
        assert connection is not None
        connection.intake_enabled = True
        webhook = await self._session.scalar(
            select(BitbucketRepositoryWebhookSecret).where(
                BitbucketRepositoryWebhookSecret.repository_id == delivery.origin_repository_id
            )
        )
        assert webhook is not None
        if webhook.confirmed_at is None:
            webhook.confirmed_at = datetime.now(UTC)
        if not unsupported:
            await notify_webhook_work(self._session)
        return True

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> WebhookReceipt | None:
        row = (
            await self._session.execute(
                select(WebhookEvent, cast(WebhookEvent.payload, Text).label("payload_json")).where(
                    WebhookEvent.provider == "bitbucket", WebhookEvent.delivery_id == delivery_id
                )
            )
        ).one_or_none()
        if row is None or row[1] is None:
            return None
        stored, payload_json = row
        candidate = WebhookReceipt(
            stored.delivery_id,
            stored.event,
            payload_json,
            "bitbucket",
            stored.provider_installation_id,
            stored.bitbucket_repository_id,
            stored.bitbucket_binding_generation,
            stored.bitbucket_webhook_generation,
        )
        active = await self._active_origin(candidate) is not None
        claimed = await super().claim(delivery_id, token, now, until)
        if claimed is not None and not active:
            await self.mark_projected(delivery_id, token, now)
            return None
        return candidate if claimed is not None else None


class SqlAlchemyBitbucketWebhookReceiptUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def receipts(self) -> SqlAlchemyBitbucketWebhookReceiptStore:
        return SqlAlchemyBitbucketWebhookReceiptStore(self.session)
