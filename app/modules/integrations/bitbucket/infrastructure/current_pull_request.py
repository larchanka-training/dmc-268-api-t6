"""Bounded current Bitbucket Cloud PR reads by immutable repository UUID."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

import httpx

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
    BitbucketProviderError,
    _check_response,
    _parse_uuid,
)
from app.modules.reviews.application.project_bitbucket_pull_request import (
    CanonicalBitbucketPullRequest,
)

_MAX_BODY_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class BitbucketTokenSource(Protocol):
    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential: ...


class BitbucketCurrentPullRequestProvider:
    def __init__(self, client: httpx.AsyncClient, credentials: BitbucketTokenSource) -> None:
        self._client = client
        self._credentials = credentials
        self._repositories = BitbucketCloudRepositoryProvider(client)

    async def get_current(
        self, target: BoundBitbucketTarget, number: int
    ) -> CanonicalBitbucketPullRequest:
        if type(number) is not int or not 0 < number <= 2147483647:
            raise ValueError("Invalid Bitbucket PR number")
        credential = await self._credentials.credential_for(target)
        repository = await self._repositories.fetch_repository_by_uuid(
            repository_uuid=target.repository_uuid, token=credential.token
        )
        url = (
            "https://api.bitbucket.org/2.0/repositories/"
            f"{repository.workspace_slug}/{repository.slug}/pullrequests/{number}"
        )
        try:
            async with self._client.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {credential.token}"},
                follow_redirects=False,
            ) as response:
                _check_response(response)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_BODY_BYTES:
                        raise BitbucketProviderError("invalid_provider_response")
                    chunks.append(chunk)
        except httpx.RequestError:
            raise BitbucketProviderError("provider_unavailable") from None
        try:
            raw = httpx.Response(200, content=b"".join(chunks)).json()
            return _parse_pull_request(raw, target.repository_uuid, number, repository.full_name)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise BitbucketProviderError("invalid_provider_response") from None


def _parse_pull_request(
    raw: object, repository_uuid: UUID, number: int, full_name: str
) -> CanonicalBitbucketPullRequest:
    if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] != number:
        raise ValueError("invalid_pr")
    destination = raw.get("destination")
    source = raw.get("source")
    if not isinstance(destination, dict) or not isinstance(source, dict):
        raise ValueError("invalid_refs")
    destination_repository = destination.get("repository")
    if not isinstance(destination_repository, dict) or (
        _parse_uuid(destination_repository.get("uuid")) != repository_uuid
    ):
        raise ValueError("invalid_origin")
    state = raw.get("state")
    if state not in {"OPEN", "MERGED", "DECLINED", "SUPERSEDED"}:
        raise ValueError("invalid_state")
    state = {"OPEN": "open", "MERGED": "merged"}.get(state, "closed")
    title = _string(raw.get("title"), 500)
    description = raw.get("description")
    if description is not None:
        description = _string(description, 65536, allow_empty=True)
    author = raw.get("author")
    login: str | None = None
    if isinstance(author, dict):
        candidate = author.get("nickname") or author.get("display_name")
        if candidate is not None:
            login = _string(candidate, 255)
    source_branch = _branch(source)
    target_branch = _branch(destination)
    head_sha = _commit(source)
    base_sha = _commit(destination)
    updated_on = raw.get("updated_on")
    if not isinstance(updated_on, str) or len(updated_on) > 64:
        raise ValueError("invalid_updated_on")
    updated_at = datetime.fromisoformat(updated_on.replace("Z", "+00:00"))
    if updated_at.tzinfo is None:
        raise ValueError("invalid_updated_on")
    return CanonicalBitbucketPullRequest(
        repository_uuid=repository_uuid,
        number=number,
        state=state,
        title=title,
        description=description,
        author_login=login,
        source_branch=source_branch,
        target_branch=target_branch,
        base_sha=base_sha,
        head_sha=head_sha,
        provider_updated_at=updated_at.astimezone(UTC),
        web_url=f"https://bitbucket.org/{full_name}/pull-requests/{number}",
    )


def _string(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not value and not allow_empty)
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValueError("invalid_string")
    return value


def _branch(ref: dict[str, object]) -> str:
    branch = ref.get("branch")
    if not isinstance(branch, dict):
        raise ValueError("invalid_branch")
    return _string(branch.get("name"), 255)


def _commit(ref: dict[str, object]) -> str:
    commit = ref.get("commit")
    if not isinstance(commit, dict) or not isinstance(commit.get("hash"), str):
        raise ValueError("invalid_commit")
    sha = commit["hash"]
    if _SHA.fullmatch(sha) is None:
        raise ValueError("invalid_commit")
    return cast(str, sha)
