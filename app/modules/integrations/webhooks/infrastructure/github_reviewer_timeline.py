"""Read explicit reviewer intent from GitHub's paginated issue timeline."""

from __future__ import annotations

from typing import Literal, Protocol

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.common.infrastructure.github_repository_path import repository_path_from_full_name
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    ReviewerTimelineIntent,
    ReviewerTimelineSnapshot,
    TimelineLifecycle,
)


class InstallationTokenProvider(Protocol):
    async def get_installation_access_token(self, installation_external_id: int) -> str: ...


class _Actor(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    type: str = Field(min_length=1)


class _Reviewer(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    login: str = Field(min_length=1)


class _ReviewEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)
    event: Literal["review_requested", "review_request_removed"]
    created_at: AwareDatetime = Field(strict=False)
    requested_reviewer: _Reviewer
    actor: _Actor


class _LifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)
    event: Literal["closed", "reopened", "merged"]
    created_at: AwareDatetime = Field(strict=False)


class HttpGitHubReviewerTimelineProvider:
    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: InstallationTokenProvider
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def snapshot(self, event: PullRequestEvent, bot_login: str) -> ReviewerTimelineSnapshot:
        full_name = event.repository_full_name
        if full_name is None:
            raise ValueError("GitHub repository name is required for timeline lookup")
        repository_path = repository_path_from_full_name(full_name)
        token = await self._tokens.get_installation_access_token(event.installation_external_id)
        latest: ReviewerTimelineIntent | None = None
        lifecycle: TimelineLifecycle | None = None
        position = 0
        path = f"{repository_path}/issues/{event.number}/timeline"
        for page in range(1, 1001):
            response = await self._client.get(
                path,
                params={"per_page": 100, "page": page},
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            items = response.json()
            if not isinstance(items, list):
                raise ValueError("GitHub timeline response must be a list")
            for item in items:
                position += 1
                if not isinstance(item, dict):
                    raise ValueError("GitHub timeline entry must be an object")
                action = item.get("event")
                if action in {"closed", "reopened", "merged"}:
                    parsed_lifecycle = _LifecycleEvent.model_validate(item)
                    if lifecycle is not None and (
                        parsed_lifecycle.created_at < lifecycle.occurred_at
                    ):
                        raise ValueError("GitHub timeline lifecycle events are out of order")
                    lifecycle = TimelineLifecycle(
                        parsed_lifecycle.id,
                        parsed_lifecycle.created_at,
                        parsed_lifecycle.event,
                        position,
                    )
                    continue
                if action not in {"review_requested", "review_request_removed"}:
                    continue
                parsed = _ReviewEvent.model_validate(item)
                if parsed.requested_reviewer.login.casefold() != bot_login.casefold():
                    continue
                # GitHub automatically removes a bot after its review; only a
                # human's explicit removal changes the persistent opt-in.
                if parsed.event == "review_request_removed" and parsed.actor.type != "User":
                    continue
                candidate = ReviewerTimelineIntent(
                    parsed.id, parsed.created_at, parsed.event == "review_requested", position
                )
                if latest is not None and candidate.occurred_at < latest.occurred_at:
                    raise ValueError("GitHub timeline review events are out of order")
                latest = candidate
            if len(items) < 100:
                break
        else:
            raise ValueError("GitHub timeline exceeded pagination bound")
        if event.action in {"review_requested", "review_request_removed"} and latest is None:
            raise ValueError("GitHub timeline has no explicit bot reviewer intent")
        return ReviewerTimelineSnapshot(latest, lifecycle)
