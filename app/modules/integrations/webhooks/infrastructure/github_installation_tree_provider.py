"""GitHub HTTP adapter for default-branch repository trees."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

import httpx
import jwt

from app.common.infrastructure.github_repository_path import (
    ref_segment,
    repository_path_from_full_name,
)
from app.modules.repositories.application.installation_repositories import (
    RepositorySnapshot,
    RepositoryTreeBlob,
)

_GITHUB_TREE_ENTRY_TYPES = {"blob", "tree", "commit"}
_GITHUB_API_VERSION = "2022-11-28"
_INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS = 60


class GitHubTreeResponseIncompleteError(RuntimeError):
    """GitHub omitted entries from a recursive tree response; delivery may retry."""


class GitHubInstallationAccessTokenProvider(Protocol):
    """Obtain a short-lived access token for an already-authorized installation."""

    async def get_installation_access_token(self, installation_external_id: int) -> str: ...


class InstallationAccessTokenCache(Protocol):
    """Cache short-lived tokens separately for each GitHub installation."""

    def get(self, installation_external_id: int) -> str | None: ...

    def set(
        self,
        installation_external_id: int,
        access_token: str,
        expires_at: datetime,
    ) -> None: ...


class InMemoryInstallationAccessTokenCache:
    """Process-local expiring cache for installation access tokens.

    The cache boundary is deliberately injectable: a deployment can replace this
    with Redis without changing the GitHub HTTP adapter.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float],
        safety_skew_seconds: float = _INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS,
    ) -> None:
        self._now = now
        self._safety_skew_seconds = safety_skew_seconds
        self._tokens: dict[int, tuple[str, float]] = {}

    def get(self, installation_external_id: int) -> str | None:
        cached = self._tokens.get(installation_external_id)
        if cached is None:
            return None
        token, expires_at = cached
        if expires_at <= self._now() + self._safety_skew_seconds:
            del self._tokens[installation_external_id]
            return None
        return token

    def set(
        self,
        installation_external_id: int,
        access_token: str,
        expires_at: datetime,
    ) -> None:
        self._tokens[installation_external_id] = (access_token, expires_at.timestamp())


GitHubAppJwtEncoder = Callable[[dict[str, object], str], str]


def _encode_github_app_jwt(claims: dict[str, object], private_key: str) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256")


class GitHubAppInstallationAccessTokenProvider:
    """Exchange a GitHub App JWT for an installation-scoped access token."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        app_id: str,
        private_key: str,
        jwt_encoder: GitHubAppJwtEncoder = _encode_github_app_jwt,
        cache: InstallationAccessTokenCache,
        now: Callable[[], float],
    ) -> None:
        self._client = client
        self._app_id = app_id
        self._private_key = private_key
        self._jwt_encoder = jwt_encoder
        self._cache = cache
        self._now = now

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        cached = self._cache.get(installation_external_id)
        if cached is not None:
            return cached

        now = int(self._now())
        app_jwt = self._jwt_encoder(
            {"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._private_key
        )
        response = await self._client.post(
            f"/app/installations/{installation_external_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
        )
        response.raise_for_status()
        access_token, expires_at = _parse_installation_access_token_response(response.json())
        if expires_at.timestamp() > self._now() + _INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS:
            self._cache.set(installation_external_id, access_token, expires_at)
        return access_token


class StaticGitHubInstallationAccessTokenProvider:
    """Expose a deployment-provided installation token through the adapter port.

    This synchronous webhook slice intentionally does not implement GitHub App
    JWT exchange.  Deployments may supply a rotated installation token while a
    dedicated credentials provider is introduced, and tests can inject any
    implementation of :class:`GitHubInstallationAccessTokenProvider`.
    """

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        del installation_external_id
        return self._access_token


def _parse_installation_access_token_response(payload: object) -> tuple[str, datetime]:
    if not isinstance(payload, Mapping):
        raise ValueError("GitHub installation token response must be an object")
    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not isinstance(token, str) or not token:
        raise ValueError("GitHub installation token response must contain a token")
    if not isinstance(expires_at, str):
        raise ValueError("GitHub installation token response must contain expires_at")
    try:
        parsed_expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("GitHub installation token expires_at must be an ISO timestamp") from error
    if parsed_expires_at.tzinfo is None:
        raise ValueError("GitHub installation token expires_at must include a timezone")
    return token, parsed_expires_at.astimezone(UTC)


class GitHubInstallationTreeProvider:
    """Fetch complete Git trees using a caller-owned, long-lived HTTP client."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: GitHubInstallationAccessTokenProvider,
    ) -> None:
        self._client = client
        self._token_provider = token_provider

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        """Fetch the recursive tree for the repository's declared default branch."""
        repository_path = repository_path_from_full_name(repository.full_name)
        branch = ref_segment(repository.default_branch)
        access_token = await self._token_provider.get_installation_access_token(
            installation_external_id
        )
        response = await self._client.get(
            f"{repository_path}/git/trees/{branch}",
            params={"recursive": "1"},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
        )
        if _is_empty_repository_answer(response):
            return ()
        response.raise_for_status()
        return _parse_tree_response(response.json())


def _is_empty_repository_answer(response: httpx.Response) -> bool:
    """409 "Git Repository is empty.": no commit yet, so no tree and no languages.

    Any other 409 (or a body of another shape) is not that answer and raises, so a retry
    can heal it instead of freezing the repository as empty.
    """
    if response.status_code != 409:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    message = body.get("message") if isinstance(body, Mapping) else None
    return isinstance(message, str) and "repository is empty" in message.lower()


def _parse_tree_response(payload: object) -> tuple[RepositoryTreeBlob, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError("GitHub tree response must be an object")
    if payload.get("truncated") is True:
        raise GitHubTreeResponseIncompleteError("GitHub returned a truncated recursive tree")
    entries = payload.get("tree")
    if not isinstance(entries, list):
        raise ValueError("GitHub tree response must contain a tree array")

    blobs: list[RepositoryTreeBlob] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("GitHub tree entries must be objects")
        path = entry.get("path")
        entry_type = entry.get("type")
        size = entry.get("size", 0)
        if not isinstance(path, str) or not path:
            raise ValueError("GitHub tree entry path must be a non-empty string")
        if entry_type not in _GITHUB_TREE_ENTRY_TYPES:
            raise ValueError("GitHub tree entry type is unsupported")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("GitHub tree entry size must be a non-negative integer")
        blobs.append(
            RepositoryTreeBlob(
                path=path,
                size=size,
                entry_type=cast(Literal["blob", "tree", "commit"], entry_type),
            )
        )
    return tuple(blobs)
