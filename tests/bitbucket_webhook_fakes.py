"""Shared Bitbucket webhook resources fake."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

from app.modules.integrations.bitbucket.application.verify_webhook import (
    verify_bitbucket_signature,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)

REMOTE = UUID("00000000-0000-4000-8000-000000000333")
WORKSPACE = UUID("00000000-0000-4000-8000-000000000334")
SECRET = "a" * 40


@dataclass
class FakeWebhookResources:
    secret: str = SECRET
    authenticated_bodies: list[bytes] = field(default_factory=list)
    receipts: list[object] = field(default_factory=list)

    async def authenticate(
        self, *, installation_id: UUID, repository_id: UUID, body: bytes, signature: str | None
    ) -> object | None:
        self.authenticated_bodies.append(body)
        if not verify_bitbucket_signature(body, signature, self.secret):
            return None
        return SimpleNamespace(
            installation_id=installation_id,
            repository_id=repository_id,
            workspace_id=WORKSPACE,
            repository_uuid=REMOTE,
            binding_generation=1,
            webhook_generation=1,
        )

    async def accept(self, receipt: object) -> InstallationDeliveryDispatchStatus:
        self.receipts.append(receipt)
        return InstallationDeliveryDispatchStatus.PENDING
