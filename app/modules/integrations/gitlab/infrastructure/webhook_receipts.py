"""GitLab admission policy over the single provider-neutral lease implementation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import Text, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.gitlab.application.webhook_receipts import (
    GitLabReceiptError,
    GitLabReceiptOriginInactive,
    validate_gitlab_receipt,
)
from app.modules.integrations.gitlab.infrastructure.models import (
    GitLabConnection,
    GitLabProjectBinding,
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


class SqlAlchemyGitLabWebhookReceiptStore(SqlAlchemyWebhookReceiptOperations):
    """Trusted internal adapter; origin is an authenticated/configured UUID, never JSON.

    Save consumes normalized admission values, not raw delivery headers. Both origin
    columns and the installation number come from the locked connection, while payload
    project IDs may only select an existing binding inside that immutable origin.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, provider="gitlab")

    async def _active_origin(self, origin: UUID, project_id: int | None) -> int | None:
        # Same workspace fence and lock order as management; no HTTP in this UOW.
        workspace_id = await self._session.scalar(
            select(ProviderInstallation.workspace_id).where(
                ProviderInstallation.id == origin, ProviderInstallation.provider == "gitlab"
            )
        )
        if workspace_id is None:
            return None
        workspace = await self._session.scalar(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            return None
        parent = await self._session.scalar(
            select(ProviderInstallation)
            .where(
                ProviderInstallation.id == origin,
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.provider == "gitlab",
            )
            .with_for_update()
        )
        if parent is None or parent.deleted_at is not None:
            return None
        connection = await self._session.scalar(
            select(GitLabConnection)
            .where(
                GitLabConnection.provider_installation_id == origin,
                GitLabConnection.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if (
            connection is None
            or connection.revoked_at is not None
            or not connection.intake_enabled
            or connection.token_ciphertext is None
            or connection.credential_key_id is None
        ):
            return None
        if project_id is not None:
            binding = await self._session.scalar(
                select(GitLabProjectBinding)
                .where(
                    GitLabProjectBinding.provider_installation_id == origin,
                    GitLabProjectBinding.instance_origin == connection.instance_origin,
                    GitLabProjectBinding.project_external_id == project_id,
                )
                .with_for_update()
            )
            if binding is None:
                return None
            repository = await self._session.scalar(
                select(Repository)
                .where(
                    Repository.id == binding.repository_id,
                    Repository.provider_installation_id == origin,
                    Repository.external_id == project_id,
                )
                .with_for_update()
            )
            if (
                repository is None
                or not repository.enabled
                or not repository.installation_available
            ):
                return None
        return parent.external_id

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> WebhookReceipt | None:
        row = (
            await self._session.execute(
                select(WebhookEvent, cast(WebhookEvent.payload, Text).label("payload_json")).where(
                    WebhookEvent.provider == "gitlab", WebhookEvent.delivery_id == delivery_id
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
            "gitlab",
            stored.provider_installation_id,
        )
        _, project_id = validate_gitlab_receipt(candidate)
        origin = candidate.origin_provider_installation_id
        assert origin is not None
        active = await self._active_origin(origin, project_id) is not None
        claimed = await super().claim(delivery_id, token, now, until)
        if claimed is not None and not active:
            # The normal lease predicates protect another claimant. Commit this
            # flush-only terminal disposition even though no dispatcher is needed.
            await self.mark_projected(delivery_id, token, now)
            return None
        # JSONB text preserves numeric precision through dispatch/replay, unlike a
        # JSONB -> Python float -> json.dumps round trip in the generic adapter.
        return replace(claimed, payload_json=payload_json) if claimed is not None else None

    async def save(self, delivery: WebhookReceipt) -> bool:
        payload, project_id = validate_gitlab_receipt(delivery)
        origin = delivery.origin_provider_installation_id
        assert origin is not None
        installation_id = await self._active_origin(origin, project_id)
        if installation_id is None:
            raise GitLabReceiptOriginInactive()
        attributes = payload.get("object_attributes")
        action = attributes.get("action") if isinstance(attributes, dict) else None
        unsupported = delivery.event_name != "Merge Request Hook"
        # Let PostgreSQL parse/compare the original validated JSON. Python's float
        # normalization could collapse different numeric payloads into a duplicate.
        canonical_payload = cast(literal(delivery.payload_json), JSONB)
        inserted = await self._session.scalar(
            insert(WebhookEvent)
            .values(
                provider="gitlab",
                provider_installation_id=origin,
                gitlab_connection_id=origin,
                installation_external_id=installation_id,
                delivery_id=delivery.delivery_id,
                event=delivery.event_name,
                action=action if isinstance(action, str) else None,
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
                    WebhookEvent.provider == "gitlab",
                    WebhookEvent.provider_installation_id == origin,
                    WebhookEvent.gitlab_connection_id == origin,
                    WebhookEvent.event == delivery.event_name,
                    WebhookEvent.payload == canonical_payload,
                )
            )
            if identical is None:
                raise GitLabReceiptError("delivery_conflict")
            return False
        if not unsupported:
            await notify_webhook_work(self._session)
        return True


class SqlAlchemyGitLabWebhookReceiptUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def receipts(self) -> SqlAlchemyGitLabWebhookReceiptStore:
        return SqlAlchemyGitLabWebhookReceiptStore(self.session)
