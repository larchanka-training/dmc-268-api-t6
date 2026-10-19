"""Trusted GitLab receipt admission values; transport authentication belongs to T7."""

from __future__ import annotations

import hashlib
import json
import math
import re
from uuid import UUID

from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt


class GitLabReceiptError(ValueError):
    """Constant public error codes; never include a header, body, or credential."""


def prepare_gitlab_receipt(
    *,
    connection_id: UUID,
    event_name: str,
    payload_json: str,
    webhook_id: str | None = None,
    idempotency_key: str | None = None,
) -> WebhookReceipt:
    """Convert a raw stable key to a stored key exactly once, before the shared engine.

    ``connection_id`` comes from trusted configuration/authenticated route selection,
    never JSON. Prefer webhook-id, with Idempotency-Key as the GitLab 17.4 fallback;
    both present must agree. Event/webhook UUIDs are intentionally not inputs.
    Stored keys are gitlab:<immutable UUID>:<sha256(raw UTF-8 stable key)>.
    Save accepts only that normalized form; claim/replay return it unchanged. Never
    feed a claimed receipt through this factory, or hash its delivery_id again.
    """
    if not isinstance(connection_id, UUID):
        raise GitLabReceiptError("invalid_origin")
    if webhook_id is not None and idempotency_key is not None and webhook_id != idempotency_key:
        raise GitLabReceiptError("invalid_delivery_identity")
    key = webhook_id if webhook_id is not None else idempotency_key
    if (
        not isinstance(key, str)
        or not re.fullmatch(r"[!-~]{1,255}", key)
        or key.startswith("gitlab:")
    ):
        raise GitLabReceiptError("invalid_delivery_identity")
    delivery_id = f"gitlab:{connection_id}:{hashlib.sha256(key.encode()).hexdigest()}"
    receipt = WebhookReceipt(delivery_id, event_name, payload_json, "gitlab", connection_id)
    validate_gitlab_receipt(receipt)
    return receipt


def validate_gitlab_receipt(receipt: WebhookReceipt) -> tuple[dict[str, object], int | None]:
    """Validate normalized storage values and project identity, without MR projection.

    Merge Request Hook is the only dispatchable event in this slice. Other object
    events may have no project (for example instance system hooks); those are durable
    ignored receipts. If a project is supplied, it must still be bound to the origin.
    Deeper MR transport/action decoding is deliberately left to T7/T8.
    """
    origin = receipt.origin_provider_installation_id
    if receipt.provider != "gitlab" or not isinstance(origin, UUID):
        raise GitLabReceiptError("invalid_origin")
    if re.fullmatch(rf"gitlab:{origin}:[0-9a-f]{{64}}", receipt.delivery_id) is None:
        raise GitLabReceiptError("invalid_delivery_identity")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,99}", receipt.event_name) is None:
        raise GitLabReceiptError("invalid_event")
    try:
        payload = json.loads(
            receipt.payload_json, parse_float=_finite_float, parse_constant=_invalid_constant
        )
    except (ValueError, RecursionError):
        raise GitLabReceiptError("invalid_payload") from None
    if not isinstance(payload, dict):
        raise GitLabReceiptError("invalid_payload")
    attributes = payload.get("object_attributes")
    action = attributes.get("action") if isinstance(attributes, dict) else None
    if isinstance(action, str) and len(action) > 100:
        raise GitLabReceiptError("invalid_payload")
    project = payload.get("project")
    project_id = None
    if "project" in payload:
        if not isinstance(project, dict):
            raise GitLabReceiptError("invalid_payload")
        project_id = project.get("id")
        if type(project_id) is not int or not 0 < project_id <= 9223372036854775807:
            raise GitLabReceiptError("invalid_payload")
    if receipt.event_name == "Merge Request Hook" and (
        project_id is None or payload.get("object_kind") != "merge_request"
    ):
        raise GitLabReceiptError("invalid_payload")
    return payload, project_id


def _invalid_constant(value: str) -> object:
    raise ValueError("invalid JSON constant")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid JSON number")
    return number
