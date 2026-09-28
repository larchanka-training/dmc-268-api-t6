"""Read authenticated user identity and App installations from GitHub."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.modules.workspaces.application.link_github_installations import (
    AuthenticatedGitHubInstallations,
    GitHubInstallation,
)

_GITHUB_API_VERSION = "2022-11-28"
_MAX_BIGINT = 2**63 - 1


class _GitHubUserDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0, le=_MAX_BIGINT)


class _GitHubAccountDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    login: str = Field(min_length=1)


class _GitHubInstallationDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0, le=_MAX_BIGINT)
    account: _GitHubAccountDto | None = None


class _GitHubInstallationPageDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    total_count: int = Field(ge=0)
    installations: list[_GitHubInstallationDto]


class _GitHubRepositoryDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0, le=_MAX_BIGINT)


class _GitHubRepositoryPageDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    total_count: int = Field(ge=0)
    repositories: list[_GitHubRepositoryDto]


class HttpGitHubUserInstallationsProvider:
    """The same user token authenticates identity and paginated installation reads."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def identify_user(self, access_token: str) -> int:
        headers = self._headers(access_token)
        user_response = await self._client.get("/user", headers=headers)
        user_response.raise_for_status()
        return _GitHubUserDto.model_validate(user_response.json()).id

    async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
        headers = self._headers(access_token)
        user_id = await self.identify_user(access_token)
        listed_installations: list[_GitHubInstallationDto] = []
        page = 1
        while True:
            response = await self._client.get(
                "/user/installations",
                params={"per_page": 100, "page": page},
                headers=headers,
            )
            response.raise_for_status()
            result = _GitHubInstallationPageDto.model_validate(response.json())
            listed_installations.extend(result.installations)
            if len(listed_installations) >= result.total_count:
                break
            if not result.installations:
                raise ValueError("GitHub installation listing ended before total_count")
            page += 1
        installations = [
            GitHubInstallation(
                item.id,
                item.account.login if item.account else "GitHub",
                await self._list_repository_ids(item.id, headers),
            )
            for item in listed_installations
        ]
        return AuthenticatedGitHubInstallations(user_id, tuple(installations))

    @staticmethod
    def _headers(access_token: str) -> dict[str, str]:
        if not access_token:
            raise ValueError("GitHub user token is required")
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }

    async def _list_repository_ids(
        self, installation_id: int, headers: dict[str, str]
    ) -> tuple[int, ...]:
        repository_ids: list[int] = []
        page = 1
        while True:
            response = await self._client.get(
                f"/user/installations/{installation_id}/repositories",
                params={"per_page": 100, "page": page},
                headers=headers,
            )
            response.raise_for_status()
            result = _GitHubRepositoryPageDto.model_validate(response.json())
            repository_ids.extend(item.id for item in result.repositories)
            if len(repository_ids) >= result.total_count:
                return tuple(repository_ids)
            if not result.repositories:
                raise ValueError("GitHub repository listing ended before total_count")
            page += 1
