"""Compose the GitHub App callback with outbound clients and the database UoW."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.auth.application.exchange_github_code import (
    ExchangeGitHubCode,
    GitHubProviderUnavailable,
)
from app.modules.auth.application.get_me import GetCurrentUser
from app.modules.auth.application.refresh_session import LogoutLocalSession, RefreshLocalSession
from app.modules.auth.infrastructure.current_user import SqlAlchemyCurrentUserRepository
from app.modules.auth.infrastructure.github_oauth import (
    HttpGitHubOAuthClient,
    HttpGitHubUserProfile,
)
from app.modules.auth.infrastructure.jwt_tokens import Rs256AccessTokenIssuer
from app.modules.workspaces.application.link_github_installations import LinkGitHubInstallations


class _CallbackInstallationLinker:
    def __init__(self, linker: LinkGitHubInstallations) -> None:
        self._linker = linker

    async def execute(
        self, access_token: str, *, expected_user_id: int | None = None
    ) -> tuple[UUID, ...]:
        try:
            return await self._linker.execute(access_token, expected_user_id=expected_user_id)
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise GitHubProviderUnavailable from exc


async def get_exchange_github_code(request: Request) -> AsyncIterator[ExchangeGitHubCode]:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    names = (
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "AUTH_JWT_PRIVATE_KEY",
        "AUTH_JWT_ISSUER",
        "AUTH_JWT_AUDIENCE",
    )
    config = {name: os.environ.get(name) for name in names}
    if not isinstance(resources, ReviewsApiResources) or any(
        not value for value in config.values()
    ):
        raise HTTPException(status_code=503, detail="GitHub authentication is not configured")
    async with (
        httpx.AsyncClient(base_url="https://github.com", timeout=10) as oauth_client,
        httpx.AsyncClient(
            base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"), timeout=10
        ) as api_client,
    ):
        yield ExchangeGitHubCode(
            oauth=HttpGitHubOAuthClient(
                oauth_client,
                client_id=config["GITHUB_CLIENT_ID"] or "",
                client_secret=config["GITHUB_CLIENT_SECRET"] or "",
            ),
            profile=HttpGitHubUserProfile(api_client),
            linker=_CallbackInstallationLinker(
                resources.github_installation_linker(client=api_client)
            ),
            uow_factory=resources.auth_session_uow,
            issuer=Rs256AccessTokenIssuer(
                config["AUTH_JWT_PRIVATE_KEY"] or "",
                issuer=config["AUTH_JWT_ISSUER"] or "",
                audience=config["AUTH_JWT_AUDIENCE"] or "",
            ),
        )


def get_refresh_local_session(request: Request) -> RefreshLocalSession:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    private_key = os.environ.get("AUTH_JWT_PRIVATE_KEY")
    issuer = os.environ.get("AUTH_JWT_ISSUER")
    audience = os.environ.get("AUTH_JWT_AUDIENCE")
    if (
        not isinstance(resources, ReviewsApiResources)
        or not private_key
        or not issuer
        or not audience
    ):
        raise HTTPException(status_code=503, detail="GitHub authentication is not configured")
    return RefreshLocalSession(
        uow_factory=resources.auth_session_uow,
        issuer=Rs256AccessTokenIssuer(private_key, issuer=issuer, audience=audience),
    )


def get_current_user(request: Request) -> GetCurrentUser:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if not isinstance(resources, ReviewsApiResources):
        raise HTTPException(status_code=503, detail="GitHub authentication is not configured")
    return GetCurrentUser(SqlAlchemyCurrentUserRepository(resources.session_factory))


def get_logout_local_session(request: Request) -> LogoutLocalSession:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if not isinstance(resources, ReviewsApiResources):
        raise HTTPException(status_code=503, detail="GitHub authentication is not configured")
    return LogoutLocalSession(uow_factory=resources.auth_session_uow)
