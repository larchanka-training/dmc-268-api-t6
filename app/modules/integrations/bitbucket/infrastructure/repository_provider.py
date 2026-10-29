"""Fixed-origin Bitbucket Cloud repository discovery and metadata reads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import httpx

from app.modules.integrations.bitbucket.application.credential_input import (
    canonical_bitbucket_slug,
    valid_bitbucket_token,
)
from app.modules.integrations.bitbucket.application.provider_error import (
    BitbucketProviderError as BitbucketProviderError,
)

_API_ORIGIN = "https://api.bitbucket.org"
_MAX_JSON_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class BitbucketRepositorySnapshot:
    uuid: UUID
    workspace_uuid: UUID
    workspace_slug: str
    slug: str
    name: str
    is_private: bool
    full_name: str
    html_url: str
    default_branch: str
    language: str | None


@dataclass(frozen=True)
class BitbucketRepositoryPage:
    repositories: tuple[BitbucketRepositorySnapshot, ...]
    next_cursor: str | None


class BitbucketCloudRepositoryProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def discover(
        self, *, workspace_slug: str, token: str, cursor: str | None = None
    ) -> BitbucketRepositoryPage:
        workspace_slug = _validate_slug(workspace_slug)
        url = (
            _validate_cursor(cursor, workspace_slug)
            if cursor is not None
            else f"{_API_ORIGIN}/2.0/repositories/{workspace_slug}?pagelen=50"
        )
        response = await self._get(url, token)
        _check_response(response)
        try:
            parsed = response.json()
        except ValueError:
            raise BitbucketProviderError("invalid_provider_response") from None
        if not isinstance(parsed, dict):
            raise BitbucketProviderError("invalid_provider_response")
        values = parsed.get("values")
        if not isinstance(values, list) or len(values) > 50:
            raise BitbucketProviderError("invalid_provider_response")
        repositories = []
        for raw in values:
            if not isinstance(raw, dict) or not isinstance(raw.get("slug"), str):
                raise BitbucketProviderError("invalid_provider_response")
            slug = _validate_slug(raw["slug"])
            repositories.append(_parse_repository(raw, workspace_slug, slug))
        next_url = parsed.get("next")
        if next_url is not None:
            next_url = _validate_cursor(next_url, workspace_slug)
        return BitbucketRepositoryPage(tuple(repositories), next_url)

    async def fetch_repository(
        self, *, workspace_slug: str, repository_slug: str, token: str
    ) -> BitbucketRepositorySnapshot:
        workspace_slug = _validate_slug(workspace_slug)
        repository_slug = _validate_slug(repository_slug)
        response = await self._get(
            f"{_API_ORIGIN}/2.0/repositories/{workspace_slug}/{repository_slug}", token
        )
        _check_response(response)
        try:
            parsed = response.json()
        except ValueError:
            raise BitbucketProviderError("invalid_provider_response") from None
        return _parse_repository(parsed, workspace_slug, repository_slug)

    async def fetch_repository_by_uuid(
        self, *, repository_uuid: UUID, token: str, workspace_uuid: UUID | None = None
    ) -> BitbucketRepositorySnapshot:
        workspace_locator = f"%7B{workspace_uuid}%7D" if workspace_uuid is not None else "%7B%7D"
        response = await self._get(
            f"{_API_ORIGIN}/2.0/repositories/{workspace_locator}/%7B{repository_uuid}%7D",
            token,
        )
        _check_response(response)
        try:
            parsed = response.json()
        except ValueError:
            raise BitbucketProviderError("invalid_provider_response") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("workspace"), dict):
            raise BitbucketProviderError("invalid_provider_response")
        workspace_slug = parsed["workspace"].get("slug")
        repository_slug = parsed.get("slug")
        if not isinstance(workspace_slug, str) or not isinstance(repository_slug, str):
            raise BitbucketProviderError("invalid_provider_response")
        remote = _parse_repository(
            parsed, _validate_slug(workspace_slug), _validate_slug(repository_slug)
        )
        if remote.uuid != repository_uuid:
            raise BitbucketProviderError("invalid_provider_response")
        return remote

    async def probe_pull_request_read(
        self, *, workspace_uuid: UUID, repository_uuid: UUID, token: str
    ) -> None:
        response = await self._get(
            f"{_API_ORIGIN}/2.0/repositories/%7B{workspace_uuid}%7D/"
            f"%7B{repository_uuid}%7D/pullrequests?pagelen=1",
            token,
        )
        try:
            parsed = response.json()
        except ValueError:
            raise BitbucketProviderError("invalid_provider_response") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("values"), list):
            raise BitbucketProviderError("invalid_provider_response")

    async def probe_current_user(self, *, token: str) -> None:
        response = await self._get(f"{_API_ORIGIN}/2.0/user", token)
        try:
            parsed = response.json()
        except ValueError:
            raise BitbucketProviderError("invalid_provider_response") from None
        if not isinstance(parsed, dict):
            raise BitbucketProviderError("invalid_provider_response")
        _parse_uuid(parsed.get("uuid"))

    async def _get(self, url: str, token: str) -> httpx.Response:
        if not valid_bitbucket_token(token):
            raise BitbucketProviderError("invalid_credential")
        try:
            async with self._client.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
            ) as response:
                _check_response(response)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_JSON_BYTES:
                        raise BitbucketProviderError("invalid_provider_response")
                    chunks.append(chunk)
                return httpx.Response(
                    response.status_code, headers=response.headers, content=b"".join(chunks)
                )
        except httpx.RequestError:
            raise BitbucketProviderError("provider_unavailable") from None


def _validate_slug(value: str) -> str:
    try:
        return canonical_bitbucket_slug(value)
    except ValueError:
        raise BitbucketProviderError("invalid_slug") from None


def _validate_cursor(value: object, workspace_slug: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or not value.isascii():
        raise BitbucketProviderError("invalid_cursor")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.bitbucket.org"
        or parsed.path != f"/2.0/repositories/{workspace_slug}"
        or not parsed.query
        or parsed.fragment
    ):
        raise BitbucketProviderError("invalid_cursor")
    try:
        pairs = parse_qsl(parsed.query, strict_parsing=True)
    except ValueError:
        raise BitbucketProviderError("invalid_cursor") from None
    if len(pairs) > 2 or len({key for key, _ in pairs}) != len(pairs):
        raise BitbucketProviderError("invalid_cursor")
    params = dict(pairs)
    if set(params) - {"page", "after", "pagelen"} or ("page" in params) == ("after" in params):
        raise BitbucketProviderError("invalid_cursor")
    if "page" in params and (
        not params["page"].isascii()
        or not params["page"].isdecimal()
        or not 1 <= int(params["page"]) <= 10000
    ):
        raise BitbucketProviderError("invalid_cursor")
    if "after" in params and (
        not 1 <= len(params["after"]) <= 512
        or not params["after"].isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in params["after"])
    ):
        raise BitbucketProviderError("invalid_cursor")
    if "pagelen" in params and (
        not params["pagelen"].isascii()
        or not params["pagelen"].isdecimal()
        or not 10 <= int(params["pagelen"]) <= 50
    ):
        raise BitbucketProviderError("invalid_cursor")
    return value


def _parse_uuid(value: object) -> UUID:
    if not isinstance(value, str) or re.fullmatch(r"\{[0-9a-fA-F-]{36}\}", value) is None:
        raise BitbucketProviderError("invalid_provider_response")
    try:
        return UUID(value[1:-1])
    except ValueError:
        raise BitbucketProviderError("invalid_provider_response") from None


def _parse_repository(
    raw: object, workspace_slug: str, repository_slug: str
) -> BitbucketRepositorySnapshot:
    if not isinstance(raw, dict):
        raise BitbucketProviderError("invalid_provider_response")
    data: dict[str, Any] = raw
    workspace = data.get("workspace")
    links = data.get("links")
    if not isinstance(workspace, dict) or not isinstance(links, dict):
        raise BitbucketProviderError("invalid_provider_response")
    html = links.get("html")
    if not isinstance(html, dict):
        raise BitbucketProviderError("invalid_provider_response")
    full_name = data.get("full_name")
    name = data.get("name")
    is_private = data.get("is_private")
    html_url = html.get("href")
    branch = data.get("mainbranch")
    default_branch = branch.get("name") if isinstance(branch, dict) else ""
    language = data.get("language")
    if (
        not isinstance(workspace.get("slug"), str)
        or workspace["slug"].lower() != workspace_slug
        or not isinstance(data.get("slug"), str)
        or data["slug"].lower() != repository_slug
        or not isinstance(full_name, str)
        or full_name.lower() != f"{workspace_slug}/{repository_slug}"
        or not isinstance(name, str)
        or not name
        or type(is_private) is not bool
        or html_url != f"https://bitbucket.org/{full_name}"
        or not isinstance(default_branch, str)
        or len(default_branch) > 255
        or (language is not None and not isinstance(language, str))
    ):
        raise BitbucketProviderError("invalid_provider_response")
    return BitbucketRepositorySnapshot(
        uuid=_parse_uuid(data.get("uuid")),
        workspace_uuid=_parse_uuid(workspace.get("uuid")),
        workspace_slug=workspace["slug"],
        slug=data["slug"],
        name=name,
        is_private=is_private,
        full_name=full_name,
        html_url=html_url,
        default_branch=default_branch,
        language=language,
    )


def _check_response(response: httpx.Response) -> None:
    if response.status_code == 200:
        return
    if response.status_code in (401, 403, 404):
        raise BitbucketProviderError("repository_inaccessible")
    if response.status_code == 429:
        raise BitbucketProviderError(
            "provider_rate_limited", retry_after=_retry_after(response.headers.get("Retry-After"))
        )
    raise BitbucketProviderError("provider_unavailable")


def _retry_after(value: str | None) -> int | None:
    if value is None or len(value) > 64 or not value.isascii():
        return None
    if value.isdecimal():
        return min(int(value), 3600)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return min(max(int((parsed - datetime.now(UTC)).total_seconds()), 0), 3600)
    except (TypeError, ValueError, OverflowError):
        return None
