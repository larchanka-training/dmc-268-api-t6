"""One-repository agent credentials, independent of browser sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.modules.auth.application.agent_tokens import (
    AgentTokenListingForbidden,
    AgentTokenRecord,
    AgentTokenRevocationForbidden,
    AgentTokens,
    OwnedAgentToken,
    RepositoryNotAvailable,
)
from app.modules.auth.application.scope import AuthScope


class FakeTokenStore:
    def __init__(self, repo_id: UUID, workspace_id: UUID) -> None:
        self.repo_id = repo_id
        self.workspace_id = workspace_id
        self.saved: dict[str, AgentTokenRecord] = {}
        self.commits = 0

    def uow(self) -> FakeTokenStore:
        return self

    @property
    def tokens(self) -> FakeTokenStore:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def authorized_repo(self, user_id: int, full_name: str) -> tuple[UUID, UUID] | None:
        if user_id == 42 and full_name == "octo/repo":
            return self.repo_id, self.workspace_id
        return None

    async def create(self, record: AgentTokenRecord) -> None:
        self.saved[record.token_hash] = record

    async def find(self, token_hash: str) -> AgentTokenRecord | None:
        return self.saved.get(token_hash)

    async def list_active(self, user_id: int, at: datetime) -> list[OwnedAgentToken]:
        return [
            OwnedAgentToken(record.id, "octo/repo", at, record.expires_at)
            for record in self.saved.values()
            if record.user_id == user_id and record.revoked_at is None and record.expires_at > at
        ]

    async def revoke(self, token_id: UUID, user_id: int, at: datetime) -> bool:
        for record in self.saved.values():
            if record.id == token_id and record.user_id == user_id:
                self.saved[record.token_hash] = AgentTokenRecord(
                    record.id,
                    record.token_hash,
                    record.user_id,
                    record.repository_id,
                    record.workspace_id,
                    record.expires_at,
                    at,
                )
                return True
        return False


def test_agent_token_is_repository_bound_hashed_and_revocable() -> None:
    now = datetime(2026, 11, 5, tzinfo=UTC)
    repo_id, workspace_id = uuid4(), uuid4()
    store = FakeTokenStore(repo_id, workspace_id)
    tokens = AgentTokens(store.uow, now=lambda: now, new_secret=lambda: "secret-once")

    issued = asyncio.run(tokens.issue(42, "octo/repo"))

    assert issued.secret == "acr_secret-once"
    assert issued.id not in (repo_id, workspace_id)
    assert len(store.saved) == 1
    assert "secret-once" not in next(iter(store.saved))
    assert issued.secret not in repr(next(iter(store.saved.values())))
    scope = asyncio.run(tokens.resolve(issued.secret))
    assert scope is not None
    assert scope.user_id == 42
    assert scope.workspace_ids == (workspace_id,)
    assert scope.agent_repository_id == repo_id

    assert asyncio.run(tokens.revoke(AuthScope(42, ()), issued.id))
    assert asyncio.run(tokens.resolve(issued.secret)) is None
    assert store.commits == 2


def test_agent_token_requires_live_repository_grant_and_expires() -> None:
    now = datetime(2026, 11, 5, tzinfo=UTC)
    store = FakeTokenStore(uuid4(), uuid4())
    tokens = AgentTokens(store.uow, now=lambda: now, new_secret=lambda: "secret")

    with pytest.raises(RepositoryNotAvailable):
        asyncio.run(tokens.issue(43, "octo/repo"))
    assert store.saved == {}

    issued = asyncio.run(tokens.issue(42, "octo/repo"))
    expired = AgentTokens(store.uow, now=lambda: now + timedelta(days=31))
    assert asyncio.run(expired.resolve(issued.secret)) is None


def test_agent_credential_cannot_revoke_a_token() -> None:
    store = FakeTokenStore(uuid4(), uuid4())
    tokens = AgentTokens(store.uow)
    scope = AuthScope(42, (), agent_repository_id=store.repo_id)

    with pytest.raises(AgentTokenRevocationForbidden):
        asyncio.run(tokens.revoke(scope, uuid4()))
    assert store.commits == 0


def test_owner_can_discover_active_token_ids_without_secrets() -> None:
    now = datetime(2026, 11, 5, tzinfo=UTC)
    store = FakeTokenStore(uuid4(), uuid4())
    tokens = AgentTokens(store.uow, now=lambda: now, new_secret=lambda: "private")
    issued = asyncio.run(tokens.issue(42, "octo/repo"))

    listed = asyncio.run(tokens.list_active(AuthScope(42, ())))
    assert [token.id for token in listed] == [issued.id]
    assert listed[0].repository == "octo/repo"
    assert "private" not in repr(listed)
    assert asyncio.run(tokens.list_active(AuthScope(43, ()))) == []
    with pytest.raises(AgentTokenListingForbidden):
        asyncio.run(tokens.list_active(AuthScope(42, (), agent_repository_id=store.repo_id)))

    assert asyncio.run(tokens.revoke(AuthScope(42, ()), issued.id))
    assert asyncio.run(tokens.list_active(AuthScope(42, ()))) == []
