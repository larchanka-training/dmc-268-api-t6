"""Bounded current Bitbucket Cloud PR reads by immutable repository UUID."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import httpx

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.commit_hash import (
    pr_commit_hash,
    resolved_commit_hash,
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
_MAX_COMMIT_PAGES = 20
_COMMIT_PAGE_SIZE = 100


class _DestinationCommitMissing(BitbucketProviderError):
    def __init__(self) -> None:
        super().__init__("repository_inaccessible")


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
        if target.bitbucket_workspace_uuid is None:
            raise BitbucketProviderError("invalid_provider_response")
        credential = await self._credentials.credential_for(target)
        repository = await self._repositories.fetch_repository_by_uuid(
            repository_uuid=target.repository_uuid,
            token=credential.token,
            workspace_uuid=target.bitbucket_workspace_uuid,
        )
        if repository.workspace_uuid != target.bitbucket_workspace_uuid:
            raise BitbucketProviderError("invalid_provider_response")
        url = (
            "https://api.bitbucket.org/2.0/repositories/"
            f"{repository.workspace_slug}/{repository.slug}/pullrequests/{number}"
        )
        try:
            raw = await self._read_json(url, credential.token)
            current = _parse_pull_request(raw, target.repository_uuid, number, repository.full_name)
            if current.head_sha is None:
                return current
            if len(current.head_sha) == 40 and len(current.base_sha) == 40:
                return current
            if not isinstance(raw, dict):
                raise ValueError("invalid_pr")
            try:
                head = await self._resolve_commit(
                    target.bitbucket_workspace_uuid,
                    target.repository_uuid,
                    current.head_sha,
                    credential.token,
                )
            except _DestinationCommitMissing:
                head = await self._resolve_pr_head(url, current.head_sha, credential.token)
            base = await self._resolve_commit(
                target.bitbucket_workspace_uuid,
                target.repository_uuid,
                current.base_sha,
                credential.token,
            )
            return replace(current, head_sha=head, base_sha=base)
        except BitbucketProviderError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError):
            raise BitbucketProviderError("invalid_provider_response") from None

    async def _resolve_commit(
        self, workspace: UUID, repository_uuid: UUID, prefix: str, token: str
    ) -> str:
        if len(prefix) == 40:
            return prefix
        url = (
            "https://api.bitbucket.org/2.0/repositories/"
            f"%7B{workspace}%7D/%7B{repository_uuid}%7D/commit/{prefix}"
        )
        raw = await self._read_json(url, token, commit_lookup=True)
        if not isinstance(raw, dict):
            raise ValueError("invalid_commit")
        return resolved_commit_hash(raw.get("hash"), prefix)

    async def _resolve_pr_head(self, pr_url: str, prefix: str, token: str) -> str:
        commits_path = urlsplit(pr_url).path + "/commits"
        url: str | None = f"https://api.bitbucket.org{commits_path}?pagelen={_COMMIT_PAGE_SIZE}"
        visited: set[str] = set()
        last_hash: str | None = None
        for _ in range(_MAX_COMMIT_PAGES):
            if url is None or url in visited:
                raise BitbucketProviderError("invalid_provider_response")
            visited.add(url)
            raw = await self._read_json(url, token)
            if not isinstance(raw, dict):
                raise ValueError("invalid_commit_page")
            values = raw.get("values")
            if not isinstance(values, list) or not values or len(values) > _COMMIT_PAGE_SIZE:
                raise ValueError("invalid_commit_page")
            for commit in values:
                if not isinstance(commit, dict):
                    raise ValueError("invalid_commit_page")
                last_hash = resolved_commit_hash(commit.get("hash"), "")
            next_url = raw.get("next")
            if next_url is None:
                if last_hash is None:
                    raise BitbucketProviderError("invalid_provider_response")
                return resolved_commit_hash(last_hash, prefix)
            url = _validate_pr_commits_cursor(next_url, commits_path)
        raise BitbucketProviderError("invalid_provider_response")

    async def _read_json(self, url: str, token: str, *, commit_lookup: bool = False) -> object:
        try:
            async with self._client.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
            ) as response:
                if commit_lookup and response.status_code == 404:
                    raise _DestinationCommitMissing()
                _check_response(response)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_BODY_BYTES:
                        raise BitbucketProviderError("invalid_provider_response")
                    chunks.append(chunk)
            return httpx.Response(200, content=b"".join(chunks)).json()
        except httpx.RequestError:
            raise BitbucketProviderError("provider_unavailable") from None


def _validate_pr_commits_cursor(value: object, commits_path: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or not value.isascii():
        raise BitbucketProviderError("invalid_provider_response")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.bitbucket.org"
        or parsed.path != commits_path
        or not parsed.query
        or parsed.fragment
    ):
        raise BitbucketProviderError("invalid_provider_response")
    try:
        pairs = parse_qsl(parsed.query, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        raise BitbucketProviderError("invalid_provider_response") from None
    params = dict(pairs)
    if (
        len(pairs) != len(params)
        or any(not item for _, item in pairs)
        or set(params) - {"page", "after", "pagelen"}
        or ("page" in params) == ("after" in params)
        or ("pagelen" in params and params["pagelen"] != str(_COMMIT_PAGE_SIZE))
    ):
        raise BitbucketProviderError("invalid_provider_response")
    if "page" in params and (
        not params["page"].isascii()
        or not params["page"].isdecimal()
        or not 1 <= int(params["page"]) <= 10000
    ):
        raise BitbucketProviderError("invalid_provider_response")
    if "after" in params and (
        not 1 <= len(params["after"]) <= 512
        or not params["after"].isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in params["after"])
    ):
        raise BitbucketProviderError("invalid_provider_response")
    return value


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
    head_sha = None if state != "open" and source.get("commit") is None else _commit(source)
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
    return pr_commit_hash(sha)
