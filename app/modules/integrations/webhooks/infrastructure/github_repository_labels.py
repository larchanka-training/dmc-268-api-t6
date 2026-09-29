"""Create the review trigger label in newly connected GitHub repositories."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot


class GitHubRepositoryLabelProvider:
    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: GitHubInstallationAccessTokenProvider
    ) -> None:
        self._client = client
        self._token_provider = token_provider

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None:
        token = await self._token_provider.get_installation_access_token(installation_external_id)
        repository_name = quote(repository.full_name, safe="/")
        response = await self._client.post(
            f"/repos/{repository_name}/labels",
            json={"name": "ai-review"},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code != 422:
            response.raise_for_status()
