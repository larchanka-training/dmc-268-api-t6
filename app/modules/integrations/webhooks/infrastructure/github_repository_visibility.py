"""GitHub HTTP adapter for the background re-read of a repository's visibility (#454)."""

from __future__ import annotations

import re

import httpx

from app.common.application.github_repository_name import (
    REPOSITORY_FULL_NAME_MAX_LENGTH,
    REPOSITORY_FULL_NAME_PATTERN,
)
from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)
from app.modules.repositories.application.repository_visibility import (
    RepositoryVisibilityReading,
    RepositoryVisibilityUnavailable,
)

_GITHUB_API_VERSION = "2022-11-28"
_FULL_NAME = re.compile(REPOSITORY_FULL_NAME_PATTERN)
# A token GitHub refuses for this installation: suspended or uninstalled. Not 401, which says
# the App's own key is wrong and nothing about the repository.
_TOKEN_REFUSED = frozenset({403, 404})


class GitHubRepositoryVisibilityReader:
    """Read by the numeric id, which a rename and a transfer do not change.

    Takes the raw token provider: its status code tells a suspended installation, which stays so,
    from an outage. Reporting the first as unavailable would keep those rows first in the queue.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: GitHubInstallationAccessTokenProvider,
    ) -> None:
        self._client = client
        self._token_provider = token_provider

    async def read(
        self, installation_external_id: int, repository_external_id: int
    ) -> RepositoryVisibilityReading | None:
        try:
            access_token = await self._token_provider.get_installation_access_token(
                installation_external_id
            )
        except InstallationAccessTokenError as error:
            if not error.transient:
                return None
            raise RepositoryVisibilityUnavailable("installation token unavailable") from error
        except httpx.HTTPStatusError as error:
            if error.response.status_code in _TOKEN_REFUSED:
                return None
            raise RepositoryVisibilityUnavailable(
                f"installation token request failed with HTTP {error.response.status_code}"
            ) from error
        except httpx.HTTPError as error:
            raise RepositoryVisibilityUnavailable(
                f"installation token request failed: {type(error).__name__}"
            ) from error
        try:
            response = await self._client.get(
                f"/repositories/{repository_external_id}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                },
            )
        except httpx.HTTPError as error:
            raise RepositoryVisibilityUnavailable(
                f"repository request failed: {type(error).__name__}"
            ) from error
        if response.status_code == 404 or (
            response.status_code == 403 and not _rate_limited(response)
        ):
            return None
        if response.status_code != 200:
            raise RepositoryVisibilityUnavailable(
                f"repository request failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise RepositoryVisibilityUnavailable("repository response is not JSON") from error
        if not isinstance(payload, dict):
            raise RepositoryVisibilityUnavailable("repository response is not an object")
        return _reading(payload)


def _rate_limited(response: httpx.Response) -> bool:
    return "retry-after" in response.headers or response.headers.get("x-ratelimit-remaining") == "0"


def _reading(payload: dict[str, object]) -> RepositoryVisibilityReading:
    private = payload.get("private")
    full_name = payload.get("full_name")
    web_url = payload.get("html_url")
    return RepositoryVisibilityReading(
        private=private if isinstance(private, bool) else None,
        full_name=(
            full_name
            if isinstance(full_name, str)
            and len(full_name) <= REPOSITORY_FULL_NAME_MAX_LENGTH
            and _FULL_NAME.fullmatch(full_name)
            else None
        ),
        web_url=web_url if isinstance(web_url, str) and web_url else None,
    )
