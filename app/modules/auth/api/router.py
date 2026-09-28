"""GitHub App callback transport contract."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.bootstrap.auth_api import (
    get_current_user,
    get_exchange_github_code,
    get_logout_local_session,
    get_refresh_local_session,
)
from app.bootstrap.portal_auth import get_auth_scope
from app.modules.auth.application.exchange_github_code import (
    REFRESH_TOKEN_LIFETIME,
    ExchangedSession,
    ExchangeGitHubCode,
    GitHubProviderUnavailable,
    InvalidGitHubCode,
)
from app.modules.auth.application.get_me import GetCurrentUser
from app.modules.auth.application.refresh_session import (
    InvalidRefreshToken,
    LogoutLocalSession,
    RefreshLocalSession,
)
from app.modules.auth.application.scope import AuthScope

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


class AuthCallbackRequestDto(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1)


class AuthUserDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    id: int
    login: str
    name: str | None
    avatar_url: str | None


class AuthSessionDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    access_token: str
    token_type: Literal["Bearer"]
    expires_in: Literal[900]
    user: AuthUserDto


class WorkspaceDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    id: UUID
    name: str
    installation_id: int


class MeDto(AuthUserDto):
    workspaces: list[WorkspaceDto]


@auth_router.get("/me", response_model=MeDto)
async def get_me(
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
    use_case: Annotated[GetCurrentUser, Depends(get_current_user)],
) -> MeDto:
    current = await use_case.execute(scope)
    if current is None:
        raise HTTPException(status_code=401, detail="authenticated user not found")
    return MeDto(
        id=current.id,
        login=current.login,
        name=current.name,
        avatar_url=current.avatar_url,
        workspaces=[
            WorkspaceDto(id=item.id, name=item.name, installation_id=item.installation_id)
            for item in current.workspaces
        ],
    )


@auth_router.post("/github/callback", response_model=AuthSessionDto)
async def exchange_github_code(
    body: AuthCallbackRequestDto,
    response: Response,
    use_case: Annotated[ExchangeGitHubCode, Depends(get_exchange_github_code)],
) -> AuthSessionDto:
    try:
        session = await use_case.execute(body.code)
    except InvalidGitHubCode as exc:
        raise HTTPException(status_code=400, detail="invalid GitHub authorization code") from exc
    except GitHubProviderUnavailable as exc:
        raise HTTPException(status_code=502, detail="GitHub authentication is unavailable") from exc
    return _session_response(session, response)


@auth_router.post("/refresh", response_model=AuthSessionDto)
async def refresh_session(
    response: Response,
    use_case: Annotated[RefreshLocalSession, Depends(get_refresh_local_session)],
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> AuthSessionDto:
    try:
        session = await use_case.execute(refresh_token)
    except InvalidRefreshToken as exc:
        raise HTTPException(status_code=401, detail="invalid refresh token") from exc
    return _session_response(session, response)


@auth_router.post("/logout", status_code=204)
async def logout_session(
    response: Response,
    use_case: Annotated[LogoutLocalSession, Depends(get_logout_local_session)],
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> Response:
    await use_case.execute(refresh_token)
    response.delete_cookie(
        key="refresh_token",
        path="/api/auth",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.status_code = 204
    return response


def _session_response(session: ExchangedSession, response: Response) -> AuthSessionDto:
    response.set_cookie(
        key="refresh_token",
        value=session.refresh_token,
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        path="/api/auth",
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return AuthSessionDto(
        access_token=session.access_token,
        token_type="Bearer",
        expires_in=900,
        user=AuthUserDto(
            id=session.user.id,
            login=session.user.login,
            name=session.user.name,
            avatar_url=session.user.avatar_url,
        ),
    )
