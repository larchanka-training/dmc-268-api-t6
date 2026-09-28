"""Raw, signature-verified webhook receipt stored for deferred replay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt


@dataclass(frozen=True)
class VerifiedGitHubDelivery:
    """JSON decoded after HMAC verification, before application event validation."""

    delivery_id: str
    event_name: str
    payload: Mapping[str, object]

    def to_receipt(self) -> WebhookReceipt:
        """Serialize the verified JSON for durable, opaque application receipt handling."""
        return WebhookReceipt(
            delivery_id=self.delivery_id,
            event_name=self.event_name,
            payload_json=json.dumps(dict(self.payload)),
        )
