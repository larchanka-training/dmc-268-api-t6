"""Exchange a GitHub App user code for a durable local session."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from app.common.application.unit_of_work import UnitOfWork
from app.modules.auth.application.refresh_token_hash import hash_refresh_token

ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
_MAX_BIGINT = 2**63 - 1


class InvalidGitHubCode(Exception):
    """GitHub rejected an expired, used, or invalid authorization code."""


class GitHubProviderUnavailable(Exception):
    """GitHub rejected the App configuration or returned an unusable response."""


class GitHubCodeExchanger(Protocol):
    async def exchange_code(self, code: str) -> str: ...


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    login: str
    name: str | None
    avatar_url: str | None


class GitHubUserProfileProvider(Protocol):
    async def get_user(self, access_token: str) -> AuthenticatedUser: ...


class InstallationLinker(Protocol):
    async def execute(
        self, access_token: str, *, expected_user_id: int | None = None
    ) -> tuple[UUID, ...]: ...


class AccessTokenIssuer(Protocol):
    def issue(self, user_id: int, workspace_ids: tuple[UUID, ...]) -> str: ...


class AuthSessionStore(Protocol):
    async def create(
        self, user: AuthenticatedUser, token_hash: str, family_id: UUID, expires_at: datetime
    ) -> None: ...


class AuthSessionUnitOfWork(UnitOfWork, Protocol):
    @property
    def sessions(self) -> AuthSessionStore: ...


@dataclass(frozen=True)
class ExchangedSession:
    access_token: str
    refresh_token: str
    user: AuthenticatedUser


class ExchangeGitHubCode:
    def __init__(
        self,
        *,
        oauth: GitHubCodeExchanger,
        profile: GitHubUserProfileProvider,
        linker: InstallationLinker,
        uow_factory: Callable[[], AuthSessionUnitOfWork],
        issuer: AccessTokenIssuer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_refresh_token: Callable[[], str] = lambda: secrets.token_urlsafe(48),
        new_family_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._oauth = oauth
        self._profile = profile
        self._linker = linker
        self._uow_factory = uow_factory
        self._issuer = issuer
        self._now = now
        self._new_refresh_token = new_refresh_token
        self._new_family_id = new_family_id

    async def execute(self, code: str) -> ExchangedSession:
        if not code:
            raise InvalidGitHubCode
        github_token = await self._oauth.exchange_code(code)
        user = await self._profile.get_user(github_token)
        if not 0 < user.id <= _MAX_BIGINT or not user.login:
            raise GitHubProviderUnavailable
        workspace_ids = await self._linker.execute(github_token, expected_user_id=user.id)
        access_token = self._issuer.issue(user.id, workspace_ids)
        refresh_token = self._new_refresh_token()
        token_hash = hash_refresh_token(refresh_token)
        async with self._uow_factory() as uow:
            await uow.sessions.create(
                user, token_hash, self._new_family_id(), self._now() + REFRESH_TOKEN_LIFETIME
            )
            await uow.commit()
        return ExchangedSession(access_token, refresh_token, user)
