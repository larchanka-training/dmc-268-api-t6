"""Rotate local refresh tokens and revoke the family on replay or logout."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.auth.application.exchange_github_code import (
    REFRESH_TOKEN_LIFETIME,
    AccessTokenIssuer,
    AuthenticatedUser,
    ExchangedSession,
)


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

    async def rotate(
        self,
        session_id: UUID,
        *,
        new_token_hash: str,
        at: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def revoke_family(self, family_id: UUID, at: datetime) -> None: ...


class RefreshSessionUnitOfWork(UnitOfWork, Protocol):
    @property
    def sessions(self) -> RefreshSessionStore: ...


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RefreshLocalSession:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], RefreshSessionUnitOfWork],
        issuer: AccessTokenIssuer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_refresh_token: Callable[[], str] = lambda: secrets.token_urlsafe(48),
    ) -> None:
        self._uow_factory = uow_factory
        self._issuer = issuer
        self._now = now
        self._new_refresh_token = new_refresh_token

    async def execute(self, refresh_token: str | None) -> ExchangedSession:
        if not refresh_token:
            raise InvalidRefreshToken
        token_hash = _token_hash(refresh_token)
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
                    new_token_hash=_token_hash(replacement),
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
            family_id = await uow.sessions.find_family_id(_token_hash(refresh_token))
            if family_id is None:
                return
            family = await uow.sessions.lock_family(family_id)
            if family is None or family.revoked_at is not None:
                return
            await uow.sessions.revoke_family(family_id, self._now())
            await uow.commit()
