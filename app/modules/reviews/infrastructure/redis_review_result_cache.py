"""Redis store for an accepted review or verifier model answer.

A miss, a bad entry, or an unreachable Redis reads as "not cached" and never
fails the caller. The same rule applies to a write. Payloads whose UTF-8 JSON
is larger than 256 KiB are not written. ``REVIEW_RESULT_CACHE_SOCKET_TIMEOUT_SECONDS``
is the socket budget for the dedicated client and for each command below.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, cast

from app.modules.reviews.application.get_run_file_lines import BLOB_CACHE_TTL

_LOGGER = logging.getLogger(__name__)

# Shared by socket_connect_timeout, socket_timeout, and asyncio.timeout.
REVIEW_RESULT_CACHE_SOCKET_TIMEOUT_SECONDS = 0.2
_MAX_VALUE_BYTES = 256 * 1024
_TTL_SECONDS = int(BLOB_CACHE_TTL.total_seconds())


class _Redis(Protocol):
    async def get(self, name: str) -> bytes | str | None: ...

    async def set(self, name: str, value: str, ex: int | None = None) -> object: ...


class RedisReviewResultCache:
    """JSON object under a caller-supplied key, kept for ``BLOB_CACHE_TTL``."""

    def __init__(self, redis: _Redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            async with asyncio.timeout(REVIEW_RESULT_CACHE_SOCKET_TIMEOUT_SECONDS):
                raw = await self._redis.get(key)
            if raw is None:
                return None
            return _json_object(raw)
        except Exception as exc:
            _log_failure("get", exc)
            return None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        try:
            payload = _payload(value)
            if payload is None:
                return
            async with asyncio.timeout(REVIEW_RESULT_CACHE_SOCKET_TIMEOUT_SECONDS):
                await self._redis.set(key, payload, ex=_TTL_SECONDS)
        except Exception as exc:
            _log_failure("set", exc)


def _payload(value: dict[str, Any]) -> str | None:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(payload.encode("utf-8")) > _MAX_VALUE_BYTES:
        return None
    return payload


def _json_object(raw: bytes | str) -> dict[str, Any]:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > _MAX_VALUE_BYTES:
        raise ValueError("review result cache payload exceeds 256 KiB")
    parsed: Any = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise ValueError("review result cache payload is not a JSON object")
    return cast(dict[str, Any], parsed)


def _log_failure(operation: str, exc: Exception) -> None:
    _LOGGER.warning(
        "review result cache %s failed error_type=%s",
        operation,
        type(exc).__name__,
    )
