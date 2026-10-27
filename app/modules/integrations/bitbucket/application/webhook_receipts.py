"""Normalized Bitbucket webhook origin and delivery identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from uuid import UUID

from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt


class BitbucketReceiptError(ValueError):
    """Public errors contain only stable codes."""


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid JSON number")
    return number


def _invalid_constant(_: str) -> object:
    raise ValueError("invalid JSON constant")


def _valid_strings(value: object) -> bool:
    # JSONB and downstream decoders have finite nesting limits. An iterative
    # walk keeps attacker-controlled nesting off the Python call stack.
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 100:
            return False
        if isinstance(current, str):
            if "\x00" in current or any(0xD800 <= ord(char) <= 0xDFFF for char in current):
                return False
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, dict):
            pending.extend((item, depth + 1) for pair in current.items() for item in pair)
    return True


@dataclass(frozen=True)
class AuthenticatedBitbucketOrigin:
    installation_id: UUID
    repository_id: UUID
    workspace_id: UUID
    repository_uuid: UUID
    binding_generation: int
    webhook_generation: int


def parse_uuid_header(value: str | None) -> UUID:
    if not isinstance(value, str) or len(value) > 40:
        raise BitbucketReceiptError("invalid_delivery_identity")
    try:
        return UUID(value.strip("{}"))
    except ValueError:
        raise BitbucketReceiptError("invalid_delivery_identity") from None


def prepare_bitbucket_receipt(
    *,
    origin: AuthenticatedBitbucketOrigin,
    event_name: str,
    hook_uuid: UUID,
    request_uuid: UUID,
    payload_json: str,
) -> WebhookReceipt:
    if (
        not isinstance(event_name, str)
        or len(event_name) > 100
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,49}:[a-z][a-z0-9_]{0,49}", event_name)
    ):
        raise BitbucketReceiptError("invalid_event")
    digest = hashlib.sha256(f"{hook_uuid}:{request_uuid}".encode()).hexdigest()
    receipt = WebhookReceipt(
        delivery_id=f"bitbucket:{origin.installation_id}:{origin.repository_id}:{digest}",
        event_name=event_name,
        payload_json=payload_json,
        provider="bitbucket",
        origin_provider_installation_id=origin.installation_id,
        origin_repository_id=origin.repository_id,
        origin_binding_generation=origin.binding_generation,
        origin_webhook_generation=origin.webhook_generation,
    )
    validate_bitbucket_receipt(receipt, origin.repository_uuid)
    return receipt


def validate_bitbucket_receipt(receipt: WebhookReceipt, repository_uuid: UUID) -> dict[str, object]:
    origin = receipt.origin_provider_installation_id
    repo = receipt.origin_repository_id
    if (
        receipt.provider != "bitbucket"
        or not isinstance(origin, UUID)
        or not isinstance(repo, UUID)
        or re.fullmatch(rf"bitbucket:{origin}:{repo}:[0-9a-f]{{64}}", receipt.delivery_id) is None
        or re.fullmatch(r"[a-z][a-z0-9_]{0,49}:[a-z][a-z0-9_]{0,49}", receipt.event_name) is None
    ):
        raise BitbucketReceiptError("invalid_origin")
    try:
        payload = json.loads(
            receipt.payload_json, parse_float=_finite_float, parse_constant=_invalid_constant
        )
    except (ValueError, RecursionError):
        raise BitbucketReceiptError("invalid_payload") from None
    if not isinstance(payload, dict) or not _valid_strings(payload):
        raise BitbucketReceiptError("invalid_payload")
    repository = payload.get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("uuid"), str):
        raise BitbucketReceiptError("invalid_payload")
    try:
        remote = UUID(repository["uuid"].strip("{}"))
    except ValueError:
        raise BitbucketReceiptError("invalid_payload") from None
    if remote != repository_uuid:
        raise BitbucketReceiptError("invalid_origin")
    return payload
