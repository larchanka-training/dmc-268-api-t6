"""Create the review trigger label in newly connected GitHub repositories."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from app.common.infrastructure.github_repository_path import repository_path_from_full_name
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot

# GitHub allows 80 content-generating requests (a POST is one) and 900 points, a POST
# costing 5, per minute: one POST every 0.8 s stays at 75 a minute, under both limits.
_MIN_LABEL_POST_INTERVAL_SECONDS = 0.8


class GitHubRepositoryLabelProvider:
    """Create the label, starting POSTs at least ``min_interval_seconds`` apart.

    The spacing is shared by every caller of one instance, so it holds across
    concurrently onboarded repositories (and events) of a process.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: GitHubInstallationAccessTokenProvider,
        min_interval_seconds: float = _MIN_LABEL_POST_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._token_provider = token_provider
        self._min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._pacing = asyncio.Lock()
        self._next_post_at = float("-inf")

    async def _wait_for_post_slot(self) -> None:
        async with self._pacing:
            await self._sleep(max(0.0, self._next_post_at - self._clock()))
            self._next_post_at = self._clock() + self._min_interval_seconds

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None:
        repository_path = repository_path_from_full_name(repository.full_name)
        token = await self._token_provider.get_installation_access_token(installation_external_id)
        await self._wait_for_post_slot()
        response = await self._client.post(
            f"{repository_path}/labels",
            json={"name": "ai-review"},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code != 422:
            response.raise_for_status()
