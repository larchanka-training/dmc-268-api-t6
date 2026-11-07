"""Shared GitLab webhook authenticator and acceptor fakes."""

from __future__ import annotations

from uuid import UUID

from app.modules.integrations.gitlab.application.authenticate_webhook import GitLabWebhookAuthStatus
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt


class Authenticator:
    def __init__(
        self, status: GitLabWebhookAuthStatus = GitLabWebhookAuthStatus.AUTHENTICATED
    ) -> None:
        self.status = status
        self.calls = 0

    async def execute(self, *, connection_id: UUID, secret: str | None) -> GitLabWebhookAuthStatus:
        self.calls += 1
        return self.status


class Acceptor:
    def __init__(self) -> None:
        self.receipts: list[WebhookReceipt] = []
        self.error: Exception | None = None
        self.status = InstallationDeliveryDispatchStatus.PENDING

    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
        if self.error is not None:
            raise self.error
        self.receipts.append(delivery)
        return self.status
