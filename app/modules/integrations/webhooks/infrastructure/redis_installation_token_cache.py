"""Redis cache of installation access tokens, shared by the worker processes.

SYSTEM_DESIGN §8.3, §10: one token per installation under ``token:{installation_id}``.
Redis is only a cache here: when it cannot be reached the process keeps working on its
own in-memory cache. The token is a secret, so no log line carries it or the traceback
of a failed command.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS,
    InMemoryInstallationAccessTokenCache,
    InstallationAccessTokenCache,
)

_LOGGER = logging.getLogger(__name__)


class RedisLike(Protocol):
    async def get(self, name: str) -> bytes | str | None: ...

    async def set(self, name: str, value: str, ex: int | None = None) -> object: ...


class RedisInstallationAccessTokenCache:
    """Tokens under ``token:{installation_id}``, expiring before GitHub revokes them.

    The key lives until ``expires_at`` minus the safety skew, so a token read from
    Redis is always still usable. Every token is also kept in the process: a failed
    read answers from there instead of minting a token on every call.
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        now: Callable[[], float],
        safety_skew_seconds: float = INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS,
    ) -> None:
        self._redis = redis
        self._now = now
        self._safety_skew_seconds = safety_skew_seconds
        self._local = InMemoryInstallationAccessTokenCache(
            now=now, safety_skew_seconds=safety_skew_seconds
        )

    @staticmethod
    def _key(installation_external_id: int) -> str:
        return f"token:{installation_external_id}"

    async def get(self, installation_external_id: int) -> str | None:
        try:
            value = await self._redis.get(self._key(installation_external_id))
        except Exception as exc:
            _LOGGER.warning(
                "Installation token cache read failed installation=%s error=%s",
                installation_external_id,
                type(exc).__name__,
            )
            return await self._local.get(installation_external_id)
        if value is None:
            return await self._local.get(installation_external_id)
        return value.decode("utf-8") if isinstance(value, bytes) else value

    async def set(
        self,
        installation_external_id: int,
        access_token: str,
        expires_at: datetime,
    ) -> None:
        await self._local.set(installation_external_id, access_token, expires_at)
        # Rounded down: the key never outlives ``expires_at`` minus the skew.
        lifetime = int(expires_at.timestamp() - self._safety_skew_seconds - self._now())
        if lifetime < 1:
            return
        try:
            await self._redis.set(self._key(installation_external_id), access_token, ex=lifetime)
        except Exception as exc:
            _LOGGER.warning(
                "Installation token cache write failed installation=%s error=%s",
                installation_external_id,
                type(exc).__name__,
            )


def installation_token_cache(
    redis: RedisLike | None, *, now: Callable[[], float] = time.time
) -> InstallationAccessTokenCache:
    """The shared Redis cache when Redis is configured, the in-memory one otherwise."""
    if redis is None:
        return InMemoryInstallationAccessTokenCache(now=now)
    return RedisInstallationAccessTokenCache(redis, now=now)
