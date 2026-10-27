"""Bounded Bitbucket Cloud HTTP reads at one fixed API origin."""

from __future__ import annotations

import json
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
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure

_ORIGIN = "https://api.bitbucket.org"
_TOTAL_BYTES = 32 * 1024 * 1024
_TIMEOUT_SECONDS = 10.0


class BitbucketRunCredentialSource(Protocol):
    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential: ...


class BitbucketRunHttpError(RunFailure):
    def __init__(self, kind: str, *, retry_after: timedelta | None = None) -> None:
        self.kind = kind
        super().__init__(
            "diff_fetch_failed", "Bitbucket review source is unavailable", retry_after=retry_after
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

    def allow_source_repository(self, source: UUID) -> None:
        if type(source) is not UUID:
            raise ValueError("invalid Bitbucket source repository")
        self._allowed.add(source)

    async def get_bytes(
        self,
        repository_uuid: UUID,
        suffix: str,
        *,
        max_bytes: int,
        params: dict[str, str] | None = None,
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
            async with self._client.stream(
                "GET",
                url,
                params=params,
                headers={"Authorization": f"Bearer {credential.token}"},
                follow_redirects=False,
                timeout=_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code == 429:
                    raw_retry = response.headers.get("Retry-After", "")
                    seconds = (
                        int(raw_retry)
                        if len(raw_retry) <= 4 and raw_retry.isascii() and raw_retry.isdecimal()
                        else 0
                    )
                    retry = timedelta(seconds=seconds) if 1 <= seconds <= 3600 else None
                    raise BitbucketRunHttpError("rate_limited", retry_after=retry)
                if response.status_code != 200:
                    raise BitbucketRunHttpError("unexpected_status")
                chunks: list[bytes] = []
                length = 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > max_bytes or self._read_bytes + length > _TOTAL_BYTES:
                        raise BitbucketRunHttpError("body_too_large")
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
        params: dict[str, str] | None = None,
    ) -> object:
        raw = await self.get_bytes(repository_uuid, suffix, max_bytes=max_bytes, params=params)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            raise BitbucketRunHttpError("invalid_json") from None
