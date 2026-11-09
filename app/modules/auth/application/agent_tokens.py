"""Issue and revoke opaque credentials for one authorized repository."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from app.common.application.unit_of_work import UnitOfWork
from app.modules.auth.application.scope import AuthScope

AGENT_TOKEN_LIFETIME = timedelta(days=30)


class RepositoryNotAvailable(Exception):
    """The user does not currently have access to the requested GitHub repository."""


class AgentTokenRevocationForbidden(Exception):
    """An agent credential cannot revoke credentials."""


class AgentTokenListingForbidden(Exception):
    """An agent credential cannot list credentials."""


@dataclass(frozen=True)
class OwnedAgentToken:
    id: UUID
    repository: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class AgentTokenRecord:
    id: UUID
    token_hash: str
    user_id: int
    repository_id: UUID
    workspace_id: UUID
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class IssuedAgentToken:
    id: UUID
    secret: str
    expires_at: datetime


class AgentTokenStore(Protocol):
    async def authorized_repo(self, user_id: int, full_name: str) -> tuple[UUID, UUID] | None: ...

    async def create(self, record: AgentTokenRecord) -> None: ...

    async def find(self, token_hash: str) -> AgentTokenRecord | None: ...

    async def list_active(self, user_id: int, at: datetime) -> list[OwnedAgentToken]: ...

    async def revoke(self, token_id: UUID, user_id: int, at: datetime) -> bool: ...


class AgentTokenUnitOfWork(UnitOfWork, Protocol):
    @property
    def tokens(self) -> AgentTokenStore: ...


class AgentTokens:
    def __init__(
        self,
        uow_factory: Callable[[], AgentTokenUnitOfWork],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_secret: Callable[[], str] = lambda: secrets.token_urlsafe(48),
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now
        self._new_secret = new_secret
        self._new_id = new_id

    async def issue(self, user_id: int, repository: str) -> IssuedAgentToken:
        secret = "acr_" + self._new_secret()
        issued_at = self._now()
        token_id = self._new_id()
        async with self._uow_factory() as uow:
            owner = await uow.tokens.authorized_repo(user_id, repository)
            if owner is None:
                raise RepositoryNotAvailable
            repository_id, workspace_id = owner
            await uow.tokens.create(
                AgentTokenRecord(
                    token_id,
                    hashlib.sha256(secret.encode()).hexdigest(),
                    user_id,
                    repository_id,
                    workspace_id,
                    issued_at + AGENT_TOKEN_LIFETIME,
                )
            )
            await uow.commit()
        return IssuedAgentToken(token_id, secret, issued_at + AGENT_TOKEN_LIFETIME)

    async def resolve(self, secret: str) -> AuthScope | None:
        if not secret.startswith("acr_"):
            return None
        async with self._uow_factory() as uow:
            record = await uow.tokens.find(hashlib.sha256(secret.encode()).hexdigest())
        if record is None or record.revoked_at is not None or record.expires_at <= self._now():
            return None
        return AuthScope(
            record.user_id,
            (record.workspace_id,),
            agent_repository_id=record.repository_id,
        )

    async def revoke(self, scope: AuthScope, token_id: UUID) -> bool:
        if scope.agent_repository_id is not None:
            raise AgentTokenRevocationForbidden
        async with self._uow_factory() as uow:
            revoked = await uow.tokens.revoke(token_id, scope.user_id, self._now())
            if revoked:
                await uow.commit()
            return revoked

    async def list_active(self, scope: AuthScope) -> list[OwnedAgentToken]:
        if scope.agent_repository_id is not None:
            raise AgentTokenListingForbidden
        async with self._uow_factory() as uow:
            return await uow.tokens.list_active(scope.user_id, self._now())
