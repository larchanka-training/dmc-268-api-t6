"""Authenticated durable GitLab receipt acknowledgement."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.bootstrap.gitlab import (
    GitLabWebhookResources,
    GitLabWebhookUnavailable,
    get_gitlab_webhook_resources,
)
from app.modules.integrations.gitlab.api.dtos import GitLabPayloadInvalid, decode_gitlab_payload
from app.modules.integrations.gitlab.application.authenticate_webhook import GitLabWebhookAuthStatus
from app.modules.integrations.gitlab.application.webhook_receipts import (
    GitLabReceiptError,
    prepare_gitlab_receipt,
)

router = APIRouter(prefix="/webhooks/gitlab")
MAX_BODY_BYTES = 10 * 1024 * 1024


def _header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise HTTPException(status_code=400, detail="gitlab_webhook_invalid_request")
    return values[0] if values else None


def _require_auth(status: GitLabWebhookAuthStatus) -> None:
    if status == GitLabWebhookAuthStatus.INVALID_CREDENTIALS:
        raise HTTPException(status_code=401, detail="gitlab_webhook_unauthorized")
    if status != GitLabWebhookAuthStatus.AUTHENTICATED:
        raise HTTPException(status_code=503, detail="gitlab_webhook_unavailable")


async def _read_body(request: Request) -> bytes:
    length = _header(request, "Content-Length")
    declared = None
    if length is not None:
        if not length or len(length) > 20 or any(char not in "0123456789" for char in length):
            raise HTTPException(status_code=400, detail="gitlab_webhook_invalid_request")
        declared = int(length)
        if declared > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="gitlab_webhook_payload_too_large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="gitlab_webhook_payload_too_large")
        body.extend(chunk)
    if declared is not None and declared != len(body):
        raise HTTPException(status_code=400, detail="gitlab_webhook_invalid_request")
    return bytes(body)


@router.post("/{installationId}", status_code=202)
async def receive_gitlab_webhook(
    installationId: str,
    request: Request,
    resources: Annotated[GitLabWebhookResources, Depends(get_gitlab_webhook_resources)],
) -> dict[str, str]:
    try:
        connection_id = UUID(installationId)
    except ValueError:
        raise HTTPException(status_code=401, detail="gitlab_webhook_unauthorized") from None
    tokens = request.headers.getlist("X-Gitlab-Token")
    if len(tokens) != 1:
        raise HTTPException(status_code=401, detail="gitlab_webhook_unauthorized")
    secret = tokens[0]
    try:
        _require_auth(await resources.authenticate(connection_id=connection_id, secret=secret))
        event = _header(request, "X-Gitlab-Event")
        if event is None:
            raise HTTPException(status_code=400, detail="gitlab_webhook_invalid_request")
        webhook_id = _header(request, "webhook-id")
        idempotency_key = _header(request, "Idempotency-Key")
        delivery = prepare_gitlab_receipt(
            connection_id=connection_id,
            event_name=event,
            webhook_id=webhook_id,
            idempotency_key=idempotency_key,
            payload_json=decode_gitlab_payload(await _read_body(request), event_name=event),
        )
        try:
            result = await resources.accept(delivery)
        except GitLabReceiptError as error:
            if str(error) == "inactive_origin":
                # accept has exited its UOW, including rollback, before this new read.
                _require_auth(
                    await resources.authenticate(connection_id=connection_id, secret=secret)
                )
            raise
        return {"status": result.value}
    except (GitLabReceiptError, GitLabPayloadInvalid):
        raise HTTPException(status_code=400, detail="gitlab_webhook_invalid_request") from None
    except GitLabWebhookUnavailable:
        raise HTTPException(status_code=503, detail="gitlab_webhook_unavailable") from None
