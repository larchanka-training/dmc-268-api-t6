"""Coordinate durable admission and the post-rollback origin race check."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.modules.integrations.gitlab.application.authenticate_webhook import GitLabWebhookAuthStatus
from app.modules.integrations.gitlab.application.webhook_receipts import GitLabReceiptOriginInactive
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt


class GitLabWebhookAuthentication(Protocol):
    async def authenticate(
        self, *, connection_id: UUID, secret: str | None
    ) -> GitLabWebhookAuthStatus: ...


class GitLabWebhookAdmission(Protocol):
    async def accept(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus: ...


class GitLabWebhookAuthenticationFailed(Exception):
    def __init__(self, status: GitLabWebhookAuthStatus) -> None:
        super().__init__("gitlab_webhook_authentication_failed")
        self.status = status


class AcceptGitLabWebhook:
    def __init__(
        self, *, authenticator: GitLabWebhookAuthentication, receiver: GitLabWebhookAdmission
    ) -> None:
        self._authenticator = authenticator
        self._receiver = receiver

    async def execute(
        self, delivery: WebhookReceipt, *, secret: str | None
    ) -> InstallationDeliveryDispatchStatus:
        try:
            return await self._receiver.accept(delivery)
        except GitLabReceiptOriginInactive:
            # Awaiting accept includes its UOW __aexit__/rollback, before this read.
            origin = delivery.origin_provider_installation_id
            if not isinstance(origin, UUID):
                raise
            status = await self._authenticator.authenticate(connection_id=origin, secret=secret)
            if status != GitLabWebhookAuthStatus.AUTHENTICATED:
                raise GitLabWebhookAuthenticationFailed(status) from None
            raise
