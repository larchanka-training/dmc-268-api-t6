"""Headless authorization exchanges GitHub credentials only on the server."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.modules.auth.application.agent_device_access import (
    AgentDeviceAccess,
    DeviceChallenge,
    DeviceFlow,
    DevicePoll,
    DevicePollTooSoon,
)
from app.modules.auth.application.agent_tokens import IssuedAgentToken
from app.modules.auth.application.exchange_github_code import AuthenticatedUser


class FakeDeviceStore:
    def __init__(self) -> None:
        self.flows: dict[str, DeviceFlow] = {}
        self.commits = 0

    def uow(self) -> FakeDeviceStore:
        return self

    @property
    def flows_store(self) -> FakeDeviceStore:
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

    async def create(self, flow: DeviceFlow) -> None:
        self.flows[flow.code_hash] = flow

    async def reserve(self, code_hash: str, at: datetime) -> DeviceFlow | None:
        flow = self.flows.get(code_hash)
        if flow is None or flow.expires_at <= at or flow.completed_at is not None:
            return None
        if flow.next_poll_at > at:
            raise DevicePollTooSoon(int((flow.next_poll_at - at).total_seconds()))
        self.flows[code_hash] = DeviceFlow(
            flow.code_hash,
            flow.repository,
            flow.expires_at,
            flow.interval,
            at + timedelta(seconds=flow.interval),
            flow.completed_at,
        )
        return flow

    async def slow_down(self, code_hash: str, interval: int, at: datetime) -> None:
        flow = self.flows[code_hash]
        self.flows[code_hash] = DeviceFlow(
            flow.code_hash,
            flow.repository,
            flow.expires_at,
            interval,
            at + timedelta(seconds=interval),
            flow.completed_at,
        )

    async def complete(self, code_hash: str, at: datetime) -> bool:
        flow = self.flows[code_hash]
        if flow.completed_at is not None:
            return False
        self.flows[code_hash] = DeviceFlow(
            flow.code_hash,
            flow.repository,
            flow.expires_at,
            flow.interval,
            flow.next_poll_at,
            at,
        )
        return True


class FakeGithub:
    def __init__(self) -> None:
        self.polls = 0

    async def start_device(self) -> DeviceChallenge:
        return DeviceChallenge(
            "device-secret", "ABCD-EFGH", "https://github.com/login/device", 900, 5
        )

    async def poll_device(self, code: str) -> DevicePoll:
        assert code == "device-secret"
        self.polls += 1
        return DevicePoll("pending") if self.polls == 1 else DevicePoll("authorized", "ghu_secret")


class FakeProfile:
    async def get_user(self, token: str) -> AuthenticatedUser:
        assert token == "ghu_secret"
        return AuthenticatedUser(42, "octocat", None, None)


class FakeLinker:
    async def execute(self, token: str, *, expected_user_id: int | None = None) -> tuple[UUID, ...]:
        assert token == "ghu_secret"
        assert expected_user_id == 42
        return ()


class FakeTokens:
    def __init__(self) -> None:
        self.issued_for: tuple[int, str] | None = None

    async def issue(self, user_id: int, repository: str) -> IssuedAgentToken:
        self.issued_for = (user_id, repository)
        return IssuedAgentToken(uuid4(), "acr_only-this-token", datetime(2026, 12, 5, tzinfo=UTC))


def test_device_flow_waits_for_user_and_returns_only_scoped_agent_token() -> None:
    clock = [datetime(2026, 11, 5, tzinfo=UTC)]
    store, github, tokens = FakeDeviceStore(), FakeGithub(), FakeTokens()
    flow = AgentDeviceAccess(
        github=github,
        profile=FakeProfile(),
        linker=FakeLinker(),
        tokens=tokens,
        uow_factory=store.uow,
        now=lambda: clock[0],
    )

    challenge = asyncio.run(flow.start("octo/repo"))
    assert challenge.user_code == "ABCD-EFGH"
    assert hashlib.sha256(challenge.device_code.encode()).hexdigest() in store.flows
    with pytest.raises(DevicePollTooSoon):
        asyncio.run(flow.poll(challenge.device_code))
    assert github.polls == 0

    clock[0] += timedelta(seconds=5)
    pending = asyncio.run(flow.poll(challenge.device_code))
    assert pending.status == "pending"
    assert pending.token is None

    clock[0] += timedelta(seconds=5)
    authorized = asyncio.run(flow.poll(challenge.device_code))
    assert authorized.status == "authorized"
    assert authorized.token is not None
    assert authorized.token.secret == "acr_only-this-token"
    assert "ghu_secret" not in repr(authorized)
    assert tokens.issued_for == (42, "octo/repo")
    assert asyncio.run(flow.poll(challenge.device_code)).status == "expired"


def test_github_slow_down_extends_poll_interval_by_at_least_five_seconds() -> None:
    class SlowGithub(FakeGithub):
        async def poll_device(self, code: str) -> DevicePoll:
            return DevicePoll("slow_down", interval=6)

    clock = [datetime(2026, 11, 5, tzinfo=UTC)]
    store = FakeDeviceStore()
    flow = AgentDeviceAccess(
        github=SlowGithub(),
        profile=FakeProfile(),
        linker=FakeLinker(),
        tokens=FakeTokens(),
        uow_factory=store.uow,
        now=lambda: clock[0],
    )
    challenge = asyncio.run(flow.start("octo/repo"))
    clock[0] += timedelta(seconds=5)
    result = asyncio.run(flow.poll(challenge.device_code))
    assert result.status == "pending"
    assert result.retry_after == 10
    with pytest.raises(DevicePollTooSoon):
        clock[0] += timedelta(seconds=6)
        asyncio.run(flow.poll(challenge.device_code))


def test_overlapping_authorized_polls_issue_only_one_agent_token() -> None:
    class OverlappingGithub(FakeGithub):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def poll_device(self, code: str) -> DevicePoll:
            self.polls += 1
            if self.polls == 1:
                self.first_started.set()
                await self.release_first.wait()
            return DevicePoll("authorized", "ghu_secret")

    class CountingTokens(FakeTokens):
        def __init__(self) -> None:
            super().__init__()
            self.issue_count = 0

        async def issue(self, user_id: int, repository: str) -> IssuedAgentToken:
            self.issue_count += 1
            return await super().issue(user_id, repository)

    async def exercise() -> None:
        clock = [datetime(2026, 11, 5, tzinfo=UTC)]
        store, github, tokens = FakeDeviceStore(), OverlappingGithub(), CountingTokens()
        flow = AgentDeviceAccess(
            github=github,
            profile=FakeProfile(),
            linker=FakeLinker(),
            tokens=tokens,
            uow_factory=store.uow,
            now=lambda: clock[0],
        )
        challenge = await flow.start("octo/repo")
        clock[0] += timedelta(seconds=5)
        first = asyncio.create_task(flow.poll(challenge.device_code))
        await github.first_started.wait()
        clock[0] += timedelta(seconds=5)
        second = await flow.poll(challenge.device_code)
        github.release_first.set()
        initial = await first
        assert {initial.status, second.status} == {"authorized", "expired"}
        assert tokens.issue_count == 1

    asyncio.run(exercise())
