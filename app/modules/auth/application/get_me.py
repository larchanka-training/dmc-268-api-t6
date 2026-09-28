"""Read the signed-in user and currently granted Workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.auth.application.scope import AuthScope


@dataclass(frozen=True)
class MyWorkspace:
    id: UUID
    name: str
    installation_id: int


@dataclass(frozen=True)
class CurrentUser:
    id: int
    login: str
    name: str | None
    avatar_url: str | None
    workspaces: tuple[MyWorkspace, ...]


class CurrentUserRepository(Protocol):
    async def get_current_user(self, scope: AuthScope) -> CurrentUser | None: ...


class GetCurrentUser:
    def __init__(self, repository: CurrentUserRepository) -> None:
        self._repository = repository

    async def execute(self, scope: AuthScope) -> CurrentUser | None:
        return await self._repository.get_current_user(scope)
