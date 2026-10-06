"""GitHub HTTP adapter for the repository fields installation events omit."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

import httpx

from app.modules.integrations.webhooks.application.installation_event_projector import (
    RepositoryDetails,
    RepositoryDetailsUnavailableError,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)

_GITHUB_API_VERSION = "2022-11-28"


class GitHubRepositoryDetailsResponseError(ValueError):
    """GitHub answered ``GET /repos/{full_name}`` without a usable branch or URL."""


class GitHubInstallationRepositoryDetailsProvider:
    """Read a repository's default branch and web URL with the installation token."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: GitHubInstallationAccessTokenProvider,
    ) -> None:
        self._client = client
        self._token_provider = token_provider

    async def fetch_repository_details(
        self, *, installation_external_id: int, full_name: str
    ) -> RepositoryDetails:
        try:
            access_token = await self._token_provider.get_installation_access_token(
                installation_external_id
            )
        except httpx.HTTPError as error:
            raise RepositoryDetailsUnavailableError(
                _unavailable_message("installation token request", error)
            ) from error
        repository_name = quote(full_name, safe="/")
        try:
            response = await self._client.get(
                f"/repos/{repository_name}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise RepositoryDetailsUnavailableError(
                _unavailable_message("repository details request", error)
            ) from error
        return _parse_repository_details(response.json())


def _unavailable_message(request: str, error: httpx.HTTPError) -> str:
    """Name the failing request and the status code or class: never URL, headers or body."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"GitHub {request} failed with HTTP {error.response.status_code}"
    return f"GitHub {request} failed: {type(error).__name__}"


def _parse_repository_details(payload: object) -> RepositoryDetails:
    if not isinstance(payload, Mapping):
        raise GitHubRepositoryDetailsResponseError("GitHub repository response must be an object")
    default_branch = payload.get("default_branch")
    web_url = payload.get("html_url")
    if not isinstance(default_branch, str) or not default_branch:
        raise GitHubRepositoryDetailsResponseError(
            "GitHub repository response must contain a default_branch"
        )
    if not isinstance(web_url, str) or not web_url:
        raise GitHubRepositoryDetailsResponseError(
            "GitHub repository response must contain an html_url"
        )
    return RepositoryDetails(default_branch=default_branch, web_url=web_url)
