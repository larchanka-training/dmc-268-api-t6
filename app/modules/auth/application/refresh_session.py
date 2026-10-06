"""Rotate local refresh tokens and revoke the family on replay or logout."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.auth.application.exchange_github_code import (
    REFRESH_TOKEN_LIFETIME,
    AccessTokenIssuer,
    AuthenticatedUser,
    ExchangedSession,
)
from app.modules.auth.application.refresh_token_hash import hash_refresh_token

DEFAULT_REFRESH_GRACE_PERIOD = timedelta(seconds=15)
MAX_GRACE_REFRESH_SESSIONS: int = 5


class InvalidRefreshToken(Exception):
    """No usable local refresh token was presented."""


@dataclass(frozen=True)
class RefreshTokenFamily:
    id: UUID
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class RefreshSessionRecord:
    id: UUID
    family_id: UUID
    user_id: int
    expires_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None


class RefreshSessionStore(Protocol):
    async def find_family_id(self, token_hash: str) -> UUID | None: ...

    async def lock_family(self, family_id: UUID) -> RefreshTokenFamily | None: ...

    async def get_session(self, token_hash: str) -> RefreshSessionRecord | None: ...

    async def current_identity(
        self, user_id: int
    ) -> tuple[AuthenticatedUser, tuple[UUID, ...]]: ...

    async def count_family_sessions(
        self,
        family_id: UUID,
        *,
        since: datetime | None = None,
        exclude_session_id: UUID | None = None,
    ) -> int: ...

    async def rotate(
        self,
        session_id: UUID,
        *,
        new_token_hash: str,
        at: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def add_session(
        self,
        family_id: UUID,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
        *,
        created_at: datetime | None = None,
    ) -> None: ...

    async def revoke_family(self, family_id: UUID, at: datetime) -> None: ...


class RefreshSessionUnitOfWork(UnitOfWork, Protocol):
    @property
    def sessions(self) -> RefreshSessionStore: ...


class RefreshLocalSession:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], RefreshSessionUnitOfWork],
        issuer: AccessTokenIssuer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_refresh_token: Callable[[], str] = lambda: secrets.token_urlsafe(48),
        grace_period: timedelta = DEFAULT_REFRESH_GRACE_PERIOD,
    ) -> None:
        self._uow_factory = uow_factory
        self._issuer = issuer
        self._now = now
        self._new_refresh_token = new_refresh_token
        self._grace_period = grace_period

    async def execute(self, refresh_token: str | None) -> ExchangedSession:
        if not refresh_token:
            raise InvalidRefreshToken
        token_hash = hash_refresh_token(refresh_token)
        replayed = False
        async with self._uow_factory() as uow:
            family_id = await uow.sessions.find_family_id(token_hash)
            if family_id is None:
                raise InvalidRefreshToken
            family = await uow.sessions.lock_family(family_id)
            if family is None or family.revoked_at is not None:
                raise InvalidRefreshToken
            session = await uow.sessions.get_session(token_hash)
            if session is None or session.family_id != family_id:
                raise InvalidRefreshToken
            at = self._now()
            if session.rotated_at is not None:
                if at - session.rotated_at <= self._grace_period:
                    count = await uow.sessions.count_family_sessions(
                        family_id, since=session.rotated_at, exclude_session_id=session.id
                    )
                    if count >= MAX_GRACE_REFRESH_SESSIONS:
                        await uow.sessions.revoke_family(family_id, at)
                        await uow.commit()
                        raise InvalidRefreshToken
                    user, workspace_ids = await uow.sessions.current_identity(session.user_id)
                    access_token = self._issuer.issue(user.id, workspace_ids)
                    replacement = self._new_refresh_token()
                    await uow.sessions.add_session(
                        family_id=family_id,
                        user_id=session.user_id,
                        token_hash=hash_refresh_token(replacement),
                        expires_at=at + REFRESH_TOKEN_LIFETIME,
                        created_at=at,
                    )
                    await uow.commit()
                    return ExchangedSession(access_token, replacement, user)
                else:
                    await uow.sessions.revoke_family(family_id, at)
                    await uow.commit()
                    replayed = True
            elif (
                session.revoked_at is not None
                or session.expires_at <= at
                or family.expires_at <= at
            ):
                raise InvalidRefreshToken
            else:
                user, workspace_ids = await uow.sessions.current_identity(session.user_id)
                access_token = self._issuer.issue(user.id, workspace_ids)
                replacement = self._new_refresh_token()
                await uow.sessions.rotate(
                    session.id,
                    new_token_hash=hash_refresh_token(replacement),
                    at=at,
                    expires_at=at + REFRESH_TOKEN_LIFETIME,
                )
                await uow.commit()
                return ExchangedSession(access_token, replacement, user)
        if replayed:
            raise InvalidRefreshToken
        raise RuntimeError("refresh decision was not completed")


class LogoutLocalSession:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], RefreshSessionUnitOfWork],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now

    async def execute(self, refresh_token: str | None) -> None:
        if not refresh_token:
            return
        async with self._uow_factory() as uow:
            family_id = await uow.sessions.find_family_id(hash_refresh_token(refresh_token))
            if family_id is None:
                return
            family = await uow.sessions.lock_family(family_id)
            if family is None or family.revoked_at is not None:
                return
            await uow.sessions.revoke_family(family_id, self._now())
            await uow.commit()
