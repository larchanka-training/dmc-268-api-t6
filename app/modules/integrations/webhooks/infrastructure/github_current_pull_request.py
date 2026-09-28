"""Current GitHub PR snapshot for ordering ambiguous webhook deliveries."""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

from app.modules.integrations.webhooks.infrastructure.github_pull_request_dtos import (
    parse_current_pull_request,
)
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
)


class InstallationTokenProvider(Protocol):
    async def get_installation_access_token(self, installation_external_id: int) -> str: ...


class HttpGitHubCurrentPullRequestProvider:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
        full_name = event.repository_full_name
        if full_name is None:
            raise ValueError("GitHub repository name is required for current PR lookup")
        owner, repo = full_name.split("/", 1)
        token = await self._tokens.get_installation_access_token(event.installation_external_id)
        response = await self._client.get(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{event.number}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return parse_current_pull_request(event, response.json())
