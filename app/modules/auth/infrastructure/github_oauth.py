"""GitHub App user-code exchange and authenticated profile reads."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.auth.application.exchange_github_code import (
    AuthenticatedUser,
    GitHubProviderUnavailable,
    InvalidGitHubCode,
)

_MAX_BIGINT = 2**63 - 1


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    access_token: str = Field(min_length=1)


class _GitHubUserResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0, le=_MAX_BIGINT)
    login: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=2048)


class HttpGitHubOAuthClient:
    def __init__(self, client: httpx.AsyncClient, *, client_id: str, client_secret: str) -> None:
        self._client = client
        self._client_id = client_id
        self._client_secret = client_secret

    async def exchange_code(self, code: str) -> str:
        try:
            response = await self._client.post(
                "/login/oauth/access_token",
                json={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise GitHubProviderUnavailable
            error = payload.get("error")
            if error == "bad_verification_code" and response.status_code < 500:
                raise InvalidGitHubCode
            if error is not None or response.status_code == 400:
                raise GitHubProviderUnavailable
            response.raise_for_status()
            return _TokenResponse.model_validate(payload).access_token
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise GitHubProviderUnavailable from exc


class HttpGitHubUserProfile:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_user(self, access_token: str) -> AuthenticatedUser:
        try:
            response = await self._client.get(
                "/user",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=10,
            )
            response.raise_for_status()
            item = _GitHubUserResponse.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise GitHubProviderUnavailable from exc
        return AuthenticatedUser(item.id, item.login, item.name, item.avatar_url)
