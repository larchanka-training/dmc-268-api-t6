"""The public Bitbucket connection list advances through bounded workspace pages."""

from __future__ import annotations

import asyncio
import base64
import json
from types import TracebackType
from typing import Self, cast
from uuid import UUID

import pytest

from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application.manage_connection import (
    BindingPosition,
    BitbucketCredentialCodec,
    BitbucketManagementError,
    BitbucketManagementStore,
    BitbucketManagementUnitOfWork,
    BitbucketRepositoryProvider,
    ManageBitbucketConnection,
    RepositoryBinding,
)

WORKSPACE = UUID(int=360)
OTHER_WORKSPACE = UUID(int=361)
INSTALLATION = UUID(int=362)


class _PagedStore:
    def __init__(self, *, member: bool = True) -> None:
        self.member = member
        self.bindings = tuple(
            RepositoryBinding(
                installation_id=INSTALLATION,
                repository_id=UUID(int=1000 + index),
                repository_uuid=UUID(int=2000 + index),
                full_name=f"team/repo{index:03d}",
                web_url=f"https://bitbucket.org/team/repo{index:03d}",
                credential_generation=1,
                binding_generation=1,
                confirmed_at=None,
            )
            for index in range(102)
        )
        self.page_calls: list[tuple[UUID, BindingPosition | None, int]] = []

    async def is_member(self, *, user_id: int, workspace_id: UUID, lock: bool = False) -> bool:
        assert user_id == 42
        assert not lock
        return self.member

    async def list_bindings_page(
        self,
        *,
        workspace_id: UUID,
        installation_id: UUID | None,
        after: BindingPosition | None,
        limit: int,
    ) -> tuple[RepositoryBinding, ...]:
        assert installation_id is None
        self.page_calls.append((workspace_id, after, limit))
        bindings = self.bindings
        if after is not None:
            bindings = tuple(
                binding
                for binding in bindings
                if (binding.full_name, binding.repository_id)
                > (after.full_name, after.repository_id)
            )
        return bindings[: limit + 1]


class _PagedUow:
    def __init__(self, store: _PagedStore) -> None:
        self._store = store

    @property
    def connections(self) -> BitbucketManagementStore:
        return cast(BitbucketManagementStore, self._store)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        raise AssertionError("read-only page committed")

    async def rollback(self) -> None:
        raise AssertionError("read-only page rolled back")


def _manager(store: _PagedStore) -> ManageBitbucketConnection:
    return ManageBitbucketConnection(
        uow_factory=lambda: cast(BitbucketManagementUnitOfWork, _PagedUow(store)),
        cipher=cast(BitbucketCredentialCodec, object()),
        provider=cast(BitbucketRepositoryProvider, object()),
        rule_sets={},
        webhook_base_url="https://portal.test",
    )


def _changed_cursor(cursor: str, **changes: object) -> str:
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    payload.update(changes)
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_connection_page_is_bounded_and_cursor_replays_the_same_position() -> None:
    store = _PagedStore()
    manager = _manager(store)
    scope = AuthScope(user_id=42, workspace_ids=(WORKSPACE,))

    async def scenario() -> None:
        first = await manager.list_connections_page(
            scope=scope, workspace_id=WORKSPACE, cursor=None
        )
        assert len(first.connections) == 100
        assert first.connections[0].full_name == "team/repo000"
        assert first.connections[-1].full_name == "team/repo099"
        assert first.next_cursor

        replay = await manager.list_connections_page(
            scope=scope, workspace_id=WORKSPACE, cursor=None
        )
        assert replay.next_cursor == first.next_cursor

        store.bindings = tuple(item for item in store.bindings if item.full_name != "team/repo099")
        second = await manager.list_connections_page(
            scope=scope, workspace_id=WORKSPACE, cursor=first.next_cursor
        )
        assert [item.full_name for item in second.connections] == [
            "team/repo100",
            "team/repo101",
        ]
        assert second.next_cursor is None
        assert [limit for _, _, limit in store.page_calls] == [100, 100, 100]
        assert store.page_calls[-1][1] == BindingPosition(
            WORKSPACE, INSTALLATION, "team/repo099", UUID(int=1099)
        )

    asyncio.run(scenario())


def test_connection_cursor_errors_and_membership_fence_precede_page_read() -> None:
    store = _PagedStore()
    manager = _manager(store)
    scope = AuthScope(user_id=42, workspace_ids=(WORKSPACE, OTHER_WORKSPACE))

    async def scenario() -> None:
        foreign = await manager.list_connections_page(
            scope=scope, workspace_id=OTHER_WORKSPACE, cursor=None
        )
        own = await manager.list_connections_page(scope=scope, workspace_id=WORKSPACE, cursor=None)
        assert foreign.next_cursor is not None
        assert own.next_cursor is not None
        store.page_calls.clear()

        for cursor, code in (
            ("%%%", "invalid_cursor"),
            (foreign.next_cursor, "invalid_cursor"),
            (_changed_cursor(own.next_cursor, v=0), "cursor_expired"),
            (_changed_cursor(own.next_cursor, name="team/repo\u0000"), "invalid_cursor"),
            (_changed_cursor(own.next_cursor, name="team/repo\ud800"), "invalid_cursor"),
        ):
            with pytest.raises(BitbucketManagementError) as raised:
                await manager.list_connections_page(
                    scope=scope, workspace_id=WORKSPACE, cursor=cursor
                )
            assert raised.value.code == code
        assert store.page_calls == []

        store.member = False
        with pytest.raises(BitbucketManagementError) as raised:
            await manager.list_connections_page(scope=scope, workspace_id=WORKSPACE, cursor="%%%")
        assert raised.value.code == "workspace_not_found"
        assert store.page_calls == []

    asyncio.run(scenario())
