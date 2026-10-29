"""Bounded Bitbucket Cloud HTTP reads at one fixed API origin."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Protocol
from uuid import UUID

import httpx

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketCredentialUnavailable,
    BitbucketRequestCredential,
    BitbucketTargetInactive,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.retry_after import parse_retry_after
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure

_ORIGIN = "https://api.bitbucket.org"
_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_REQUESTS = 800
# Context excerpts are optional. Leave half the attempt budget for the bounded
# conventions tree, commit prefix and canonical checks that follow the context stage.
_MAX_CONTEXT_REQUESTS = 400
_TIMEOUT_SECONDS = 10.0


class BitbucketRunCredentialSource(Protocol):
    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential: ...


class BitbucketRunHttpError(RunFailure):
    def __init__(
        self,
        kind: str,
        *,
        retry_after: timedelta | None = None,
        status_code: int | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        code = (
            "bitbucket_response_budget_exceeded"
            if kind == "total_budget_exceeded"
            else "bitbucket_request_budget_exceeded"
            if kind == "request_budget_exceeded"
            else "bitbucket_invalid_response"
            if kind == "invalid_json"
            else "bitbucket_fetch_failed"
        )
        super().__init__(
            code,
            "Bitbucket response budget is exhausted"
            if kind == "total_budget_exceeded"
            else "Bitbucket request budget is exhausted"
            if kind == "request_budget_exceeded"
            else "Bitbucket response is malformed"
            if kind == "invalid_json"
            else "Bitbucket review source is unavailable",
            retry_after=retry_after,
            rate_limited=kind == "rate_limited",
        )


class BitbucketRunHttp:
    """Only UUID repository paths are emitted; response URLs never become requests."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: BitbucketRunCredentialSource,
        target: BoundBitbucketTarget,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self.target = target
        self._allowed = {target.repository_uuid}
        self._read_bytes = 0
        self._requests = 0
        self._context_limit: int | None = None

    @property
    def request_count(self) -> int:
        return self._requests

    @contextmanager
    def optional_context_budget(self) -> Iterator[None]:
        previous_limit = self._context_limit
        self._context_limit = _MAX_CONTEXT_REQUESTS
        try:
            yield
        finally:
            self._context_limit = previous_limit

    def allow_source_repository(self, source: UUID) -> None:
        if type(source) is not UUID:
            raise ValueError("invalid Bitbucket source repository")
        self._allowed.add(source)

    async def checkpoint(self) -> None:
        """Recheck the persisted binding before serving cached provider data."""
        try:
            await self._credentials.credential_for(self.target)
        except (
            BitbucketTargetInactive,
            BitbucketCredentialGenerationChanged,
            BitbucketBindingGenerationChanged,
            BitbucketCredentialUnavailable,
        ):
            raise RunCancelled("installation_unavailable") from None

    async def get_bytes(
        self,
        repository_uuid: UUID,
        suffix: str,
        *,
        max_bytes: int,
        params: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
    ) -> bytes:
        if repository_uuid not in self._allowed:
            raise RunCancelled("installation_unavailable")
        if (
            not isinstance(suffix, str)
            or not suffix
            or suffix.startswith("/")
            or "\\" in suffix
            or "?" in suffix
            or "#" in suffix
            or (
                suffix.endswith("/")
                and not (
                    suffix.startswith("src/")
                    and len(suffix.split("/")) >= 3
                    and len(suffix.split("/")[1]) == 40
                    and all(char in "0123456789abcdef" for char in suffix.split("/")[1])
                )
            )
            or any(segment in {"", ".", ".."} for segment in suffix.rstrip("/").split("/"))
            or not 0 < max_bytes <= _TOTAL_BYTES
        ):
            raise ValueError("invalid Bitbucket run path")
        if self._requests >= min(_MAX_REQUESTS, self._context_limit or _MAX_REQUESTS):
            raise BitbucketRunHttpError("request_budget_exceeded")
        try:
            credential = await self._credentials.credential_for(self.target)
        except (
            BitbucketTargetInactive,
            BitbucketCredentialGenerationChanged,
            BitbucketBindingGenerationChanged,
            BitbucketCredentialUnavailable,
        ):
            raise RunCancelled("installation_unavailable") from None
        url = f"{_ORIGIN}/2.0/repositories/%7B%7D/%7B{repository_uuid}%7D/{suffix}"
        try:
            self._requests += 1
            async with self._client.stream(
                "GET",
                url,
                params=params,
                headers={"Authorization": f"Bearer {credential.token}"},
                follow_redirects=False,
                timeout=_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code == 429:
                    raise BitbucketRunHttpError(
                        "rate_limited",
                        retry_after=parse_retry_after(response.headers.get("Retry-After"))
                        or timedelta(hours=1),
                        status_code=429,
                    )
                if response.status_code != 200:
                    raise BitbucketRunHttpError(
                        "unexpected_status", status_code=response.status_code
                    )
                chunks: list[bytes] = []
                length = 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > max_bytes:
                        raise BitbucketRunHttpError("body_too_large")
                    if self._read_bytes + length > _TOTAL_BYTES:
                        raise BitbucketRunHttpError("total_budget_exceeded")
                    chunks.append(chunk)
        except httpx.RequestError:
            raise BitbucketRunHttpError("request_failed") from None
        self._read_bytes += length
        return b"".join(chunks)

    async def get_json(
        self,
        repository_uuid: UUID,
        suffix: str,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        params: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
    ) -> object:
        raw = await self.get_bytes(repository_uuid, suffix, max_bytes=max_bytes, params=params)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            raise BitbucketRunHttpError("invalid_json") from None
