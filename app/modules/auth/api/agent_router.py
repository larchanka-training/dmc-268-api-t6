"""Headless GitHub consent, repository token exchange and revocation."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.bootstrap.agent_access import get_agent_device_access, get_agent_tokens
from app.bootstrap.portal_auth import get_auth_scope
from app.common.application.github_repository_name import (
    REPOSITORY_FULL_NAME_MAX_LENGTH,
    REPOSITORY_FULL_NAME_PATTERN,
)
from app.common.strict_json import StrictJsonRoute
from app.modules.auth.application.agent_device_access import (
    AgentDeviceAccess,
    DevicePollTooSoon,
)
from app.modules.auth.application.agent_tokens import AgentTokens, RepositoryNotAvailable
from app.modules.auth.application.exchange_github_code import GitHubProviderUnavailable
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.api.dtos import UtcDatetime

agent_router = APIRouter(prefix="/api/agent", tags=["agent"], route_class=StrictJsonRoute)


@agent_router.get("/instructions", response_class=Response)
async def agent_instructions() -> Response:
    content = Path(__file__).with_name("agent_instructions.md").read_text(encoding="utf-8")
    return Response(content, media_type="text/markdown")


class DeviceStartRequestDto(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repository: str = Field(
        min_length=3,
        max_length=REPOSITORY_FULL_NAME_MAX_LENGTH,
        pattern=REPOSITORY_FULL_NAME_PATTERN,
    )


class DeviceChallengeDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DevicePollRequestDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    device_code: str = Field(min_length=1)


class DevicePollResponseDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    status: Literal["pending", "authorized", "denied", "expired"]
    retry_after: int | None = None
    token: str | None = None
    token_id: UUID | None = None
    expires_at: UtcDatetime | None = None


@agent_router.post("/device/start", response_model=DeviceChallengeDto)
async def start_device_authorization(
    body: DeviceStartRequestDto,
    use_case: Annotated[AgentDeviceAccess, Depends(get_agent_device_access)],
) -> DeviceChallengeDto:
    try:
        challenge = await use_case.start(body.repository)
    except GitHubProviderUnavailable as exc:
        raise HTTPException(
            status_code=502, detail="GitHub device authorization is unavailable"
        ) from exc
    return DeviceChallengeDto(
        device_code=challenge.device_code,
        user_code=challenge.user_code,
        verification_uri=challenge.verification_uri,
        expires_in=challenge.expires_in,
        interval=challenge.interval,
    )


@agent_router.post("/device/poll", response_model=DevicePollResponseDto)
async def poll_device_authorization(
    body: DevicePollRequestDto,
    use_case: Annotated[AgentDeviceAccess, Depends(get_agent_device_access)],
) -> DevicePollResponseDto:
    try:
        outcome = await use_case.poll(body.device_code)
    except DevicePollTooSoon as exc:
        raise HTTPException(
            status_code=429,
            detail="device authorization polled too soon",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except RepositoryNotAvailable as exc:
        raise HTTPException(
            status_code=404, detail="repository is not available to this user"
        ) from exc
    except (GitHubProviderUnavailable, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail="GitHub device authorization is unavailable"
        ) from exc
    return DevicePollResponseDto(
        status=outcome.status,
        retry_after=outcome.retry_after,
        token=outcome.token.secret if outcome.token is not None else None,
        token_id=outcome.token.id if outcome.token is not None else None,
        expires_at=outcome.token.expires_at if outcome.token is not None else None,
    )


@agent_router.delete("/tokens/{token_id}", status_code=204)
async def revoke_agent_token(
    token_id: UUID,
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
    tokens: Annotated[AgentTokens, Depends(get_agent_tokens)],
) -> Response:
    if scope.agent_repository_id is not None:
        raise HTTPException(status_code=403, detail="agent token cannot revoke other tokens")
    if not await tokens.revoke(scope.user_id, token_id):
        raise HTTPException(status_code=404, detail="agent token not found")
    return Response(status_code=204)
