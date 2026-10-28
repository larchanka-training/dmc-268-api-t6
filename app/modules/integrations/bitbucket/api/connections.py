"""Workspace-scoped Bitbucket browser connection routes."""

from __future__ import annotations

from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import ConfigDict, Field, field_validator

from app.bootstrap.bitbucket import get_bitbucket_management, get_bitbucket_workspace_scope
from app.bootstrap.portal_auth import get_auth_scope
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketManagementError,
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketProviderError,
)
from app.modules.reviews.api.dtos import ApiDto

router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/bitbucket",
    tags=["bitbucket"],
    dependencies=[Depends(get_auth_scope)],
)

_WEBHOOK_EVENTS = [
    "repo:push",
    "pullrequest:created",
    "pullrequest:updated",
    "pullrequest:fulfilled",
    "pullrequest:rejected",
]


class _TokenRequest(ApiDto):
    model_config = ConfigDict(extra="forbid")

    api_token: str = Field(
        min_length=1,
        max_length=4096,
        # Pydantic's regex engine does not support the JSON Schema end-of-input lookahead.
        json_schema_extra={"pattern": r"^[!-~]+(?![\s\S])", "writeOnly": True},
    )

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, value: str) -> str:
        if not all(33 <= ord(character) <= 126 for character in value):
            raise ValueError("invalid Bitbucket token")
        return value


class ConnectRequest(_TokenRequest):
    bitbucket_workspace: str
    repository_slug: str
    repository_uuid: UUID


class CredentialRotateRequest(_TokenRequest):
    pass


class CredentialRotateResponse(ApiDto):
    id: UUID
    credential_generation: int


class DiscoveryRequest(_TokenRequest):
    bitbucket_workspace: str
    cursor: str | None = None


class DiscoveredRepository(ApiDto):
    uuid: UUID
    workspace_slug: str
    slug: str
    name: str
    is_private: bool
    html_url: str


class DiscoveryResponse(ApiDto):
    repositories: list[DiscoveredRepository]
    next_cursor: str | None


class WebhookSetup(ApiDto):
    url: str
    events: list[str]
    secret: str


class ConnectResponse(ApiDto):
    id: UUID
    repository_id: UUID
    repository_uuid: UUID
    workspace_slug: str
    repository_slug: str
    status: Literal["awaiting_webhook", "active"]
    credential_generation: int
    webhook: WebhookSetup | None


class ConnectionItem(ApiDto):
    id: UUID
    repository_id: UUID
    repository_uuid: UUID
    workspace_slug: str
    repository_slug: str
    status: Literal["awaiting_webhook", "active"]
    credential_generation: int
    webhook_url: str


class ConnectionList(ApiDto):
    connections: list[ConnectionItem]
    next_cursor: str | None


def _raise_management(error: BitbucketManagementError) -> NoReturn:
    status = {
        "workspace_not_found": 404,
        "repository_uuid_mismatch": 409,
        "credential_rotation_required": 409,
        "invalid_credential": 400,
        "credential_unavailable": 503,
        "repository_not_found": 404,
        "connection_not_found": 404,
    }.get(error.code, 409)
    raise HTTPException(status_code=status, detail=error.code) from None


def _raise_provider(error: BitbucketProviderError) -> NoReturn:
    status = {
        "invalid_slug": 400,
        "invalid_cursor": 400,
        "repository_inaccessible": 422,
        "provider_rate_limited": 429,
    }.get(error.code, 502)
    headers = (
        {"Retry-After": str(error.retry_after)}
        if error.code == "provider_rate_limited" and error.retry_after is not None
        else None
    )
    raise HTTPException(status_code=status, detail=error.code, headers=headers) from None


@router.post("/discovery", response_model=DiscoveryResponse)
async def discover_bitbucket_repositories(
    workspace_id: UUID,
    body: DiscoveryRequest,
    scope: Annotated[AuthScope, Depends(get_bitbucket_workspace_scope)],
    manager: Annotated[ManageBitbucketConnection, Depends(get_bitbucket_management)],
) -> DiscoveryResponse:
    try:
        page = await manager.discover(
            scope=scope,
            workspace_id=workspace_id,
            workspace_slug=body.bitbucket_workspace,
            token=body.api_token,
            cursor=body.cursor,
        )
    except BitbucketManagementError as error:
        _raise_management(error)
    except BitbucketProviderError as error:
        _raise_provider(error)
    return DiscoveryResponse(
        repositories=[
            DiscoveredRepository(
                uuid=repository.uuid,
                workspace_slug=repository.workspace_slug,
                slug=repository.slug,
                name=repository.name,
                is_private=repository.is_private,
                html_url=repository.html_url,
            )
            for repository in page.repositories
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/connections", response_model=ConnectionList)
async def list_bitbucket_connections(
    workspace_id: UUID,
    scope: Annotated[AuthScope, Depends(get_bitbucket_workspace_scope)],
    manager: Annotated[ManageBitbucketConnection, Depends(get_bitbucket_management)],
) -> ConnectionList:
    try:
        bindings = await manager.list_connections(scope=scope, workspace_id=workspace_id)
    except BitbucketManagementError as error:
        _raise_management(error)
    return ConnectionList(
        connections=[
            ConnectionItem(
                id=item.installation_id,
                repository_id=item.repository_id,
                repository_uuid=item.repository_uuid,
                workspace_slug=item.full_name.split("/", 1)[0],
                repository_slug=item.full_name.split("/", 1)[1],
                status="active" if item.confirmed_at is not None else "awaiting_webhook",
                credential_generation=item.credential_generation,
                webhook_url=manager.webhook_url(item.installation_id, item.repository_id),
            )
            for item in bindings
        ],
        next_cursor=None,
    )


@router.post("/connections", response_model=ConnectResponse, status_code=201)
async def connect_bitbucket_repository(
    workspace_id: UUID,
    body: ConnectRequest,
    scope: Annotated[AuthScope, Depends(get_bitbucket_workspace_scope)],
    manager: Annotated[ManageBitbucketConnection, Depends(get_bitbucket_management)],
) -> ConnectResponse:
    try:
        result = await manager.connect(
            scope=scope,
            workspace_id=workspace_id,
            workspace_slug=body.bitbucket_workspace,
            repository_slug=body.repository_slug,
            repository_uuid=body.repository_uuid,
            token=body.api_token,
        )
    except BitbucketManagementError as error:
        _raise_management(error)
    except BitbucketProviderError as error:
        _raise_provider(error)
    webhook = (
        WebhookSetup(
            url=manager.webhook_url(result.installation_id, result.repository_id),
            events=_WEBHOOK_EVENTS,
            secret=result.webhook_secret,
        )
        if result.webhook_secret is not None
        else None
    )
    return ConnectResponse(
        id=result.installation_id,
        repository_id=result.repository_id,
        repository_uuid=result.repository_uuid,
        workspace_slug=result.workspace_slug,
        repository_slug=result.repository_slug,
        status="active" if result.confirmed else "awaiting_webhook",
        credential_generation=result.credential_generation,
        webhook=webhook,
    )


@router.delete("/connections/{installation_id}/repositories/{repository_id}", status_code=204)
async def disconnect_bitbucket_repository(
    workspace_id: UUID,
    installation_id: UUID,
    repository_id: UUID,
    scope: Annotated[AuthScope, Depends(get_bitbucket_workspace_scope)],
    manager: Annotated[ManageBitbucketConnection, Depends(get_bitbucket_management)],
) -> Response:
    try:
        await manager.disconnect(
            scope=scope,
            workspace_id=workspace_id,
            installation_id=installation_id,
            repository_id=repository_id,
        )
    except BitbucketManagementError as error:
        _raise_management(error)
    return Response(status_code=204)


@router.put(
    "/connections/{installation_id}/credential",
    response_model=CredentialRotateResponse,
)
async def rotate_bitbucket_credential(
    workspace_id: UUID,
    installation_id: UUID,
    body: CredentialRotateRequest,
    scope: Annotated[AuthScope, Depends(get_bitbucket_workspace_scope)],
    manager: Annotated[ManageBitbucketConnection, Depends(get_bitbucket_management)],
) -> CredentialRotateResponse:
    try:
        result = await manager.rotate_token(
            scope=scope,
            workspace_id=workspace_id,
            installation_id=installation_id,
            token=body.api_token,
        )
    except BitbucketManagementError as error:
        _raise_management(error)
    except BitbucketProviderError as error:
        _raise_provider(error)
    return CredentialRotateResponse(
        id=result.installation_id,
        credential_generation=result.generation,
    )


@router.post(
    "/connections/{installation_id}/repositories/{repository_id}/webhook-secret",
    response_model=WebhookSetup,
)
async def rotate_bitbucket_repository_webhook_secret(
    workspace_id: UUID,
    installation_id: UUID,
    repository_id: UUID,
    scope: Annotated[AuthScope, Depends(get_bitbucket_workspace_scope)],
    manager: Annotated[ManageBitbucketConnection, Depends(get_bitbucket_management)],
) -> WebhookSetup:
    try:
        result = await manager.rotate_webhook_secret(
            scope=scope,
            workspace_id=workspace_id,
            installation_id=installation_id,
            repository_id=repository_id,
        )
    except BitbucketManagementError as error:
        _raise_management(error)
    return WebhookSetup(url=result.url, events=_WEBHOOK_EVENTS, secret=result.secret)
