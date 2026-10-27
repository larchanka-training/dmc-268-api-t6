"""Raw-byte Bitbucket Cloud webhook ingress."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.bootstrap.bitbucket import (
    BitbucketWebhookResources,
    BitbucketWebhookUnavailable,
    get_bitbucket_webhook_resources,
)
from app.modules.integrations.bitbucket.application.webhook_receipts import (
    BitbucketReceiptError,
    parse_uuid_header,
    prepare_bitbucket_receipt,
)

router = APIRouter(prefix="/webhooks/bitbucket")
MAX_BODY_BYTES = 256 * 1024


def _header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise HTTPException(status_code=400, detail="bitbucket_webhook_invalid_request")
    return values[0] if values else None


async def _read_body(request: Request) -> bytes:
    length = _header(request, "Content-Length")
    if length is not None:
        if not length or len(length) > 20 or not length.isascii() or not length.isdecimal():
            raise HTTPException(status_code=400, detail="bitbucket_webhook_invalid_request")
        if int(length) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="bitbucket_webhook_payload_too_large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="bitbucket_webhook_payload_too_large")
        body.extend(chunk)
    if length is not None and int(length) != len(body):
        raise HTTPException(status_code=400, detail="bitbucket_webhook_invalid_request")
    return bytes(body)


@router.post("/{installation_id}/{repository_id}", status_code=202)
async def receive_bitbucket_webhook(
    installation_id: str,
    repository_id: str,
    request: Request,
    resources: Annotated[BitbucketWebhookResources, Depends(get_bitbucket_webhook_resources)],
) -> dict[str, str]:
    try:
        installation = UUID(installation_id)
        repository = UUID(repository_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="bitbucket_webhook_unauthorized") from None
    signature = _header(request, "X-Hub-Signature")
    body = await _read_body(request)
    try:
        origin = await resources.authenticate(
            installation_id=installation, repository_id=repository, body=body, signature=signature
        )
    except BitbucketWebhookUnavailable:
        raise HTTPException(status_code=503, detail="bitbucket_webhook_unavailable") from None
    if origin is None:
        raise HTTPException(status_code=401, detail="bitbucket_webhook_unauthorized")
    try:
        event = _header(request, "X-Event-Key")
        hook = parse_uuid_header(_header(request, "X-Hook-UUID"))
        delivery = parse_uuid_header(_header(request, "X-Request-UUID"))
        attempt = _header(request, "X-Attempt-Number")
        if attempt is not None and (
            len(attempt) > 4
            or not attempt.isascii()
            or not attempt.isdecimal()
            or not 1 <= int(attempt) <= 1000
        ):
            raise BitbucketReceiptError("invalid_attempt")
        receipt = prepare_bitbucket_receipt(
            origin=origin,
            event_name=event or "",
            hook_uuid=hook,
            request_uuid=delivery,
            payload_json=body.decode("utf-8"),
        )
        result = await resources.accept(receipt)
    except BitbucketReceiptError as error:
        if str(error) == "inactive_origin":
            raise HTTPException(status_code=401, detail="bitbucket_webhook_unauthorized") from None
        if str(error) == "delivery_conflict":
            raise HTTPException(status_code=409, detail="bitbucket_webhook_conflict") from None
        raise HTTPException(status_code=400, detail="bitbucket_webhook_invalid_request") from None
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="bitbucket_webhook_invalid_request") from None
    except BitbucketWebhookUnavailable:
        raise HTTPException(status_code=503, detail="bitbucket_webhook_unavailable") from None
    return {"status": result.value}
