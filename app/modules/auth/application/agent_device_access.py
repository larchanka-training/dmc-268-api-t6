"""Authorize a headless agent through GitHub without exposing GitHub credentials."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from app.common.application.unit_of_work import UnitOfWork
from app.modules.auth.application.agent_tokens import IssuedAgentToken
from app.modules.auth.application.exchange_github_code import (
    GitHubUserProfileProvider,
    InstallationLinker,
)


class DevicePollTooSoon(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("device authorization polled too soon")
        self.retry_after = retry_after


@dataclass(frozen=True)
class DeviceChallenge:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DevicePoll:
    status: Literal["pending", "slow_down", "authorized", "denied", "expired"]
    access_token: str | None = None
    interval: int | None = None


@dataclass(frozen=True)
class DeviceFlow:
    code_hash: str
    repository: str
    expires_at: datetime
    interval: int
    next_poll_at: datetime
    completed_at: datetime | None = None


@dataclass(frozen=True)
class DeviceOutcome:
    status: Literal["pending", "authorized", "denied", "expired"]
    token: IssuedAgentToken | None = None
    retry_after: int | None = None


class DeviceOAuth(Protocol):
    async def start_device(self) -> DeviceChallenge: ...

    async def poll_device(self, code: str) -> DevicePoll: ...


class AgentTokenIssuer(Protocol):
    async def issue(self, user_id: int, repository: str) -> IssuedAgentToken: ...


class DeviceFlowStore(Protocol):
    async def create(self, flow: DeviceFlow) -> None: ...

    async def reserve(self, code_hash: str, at: datetime) -> DeviceFlow | None: ...

    async def slow_down(self, code_hash: str, interval: int, at: datetime) -> None: ...

    async def complete(self, code_hash: str, at: datetime) -> bool: ...


class DeviceFlowUnitOfWork(UnitOfWork, Protocol):
    @property
    def flows_store(self) -> DeviceFlowStore: ...


class AgentDeviceAccess:
    def __init__(
        self,
        *,
        github: DeviceOAuth,
        profile: GitHubUserProfileProvider,
        linker: InstallationLinker,
        tokens: AgentTokenIssuer,
        uow_factory: Callable[[], DeviceFlowUnitOfWork],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._github = github
        self._profile = profile
        self._linker = linker
        self._tokens = tokens
        self._uow_factory = uow_factory
        self._now = now

    async def start(self, repository: str) -> DeviceChallenge:
        challenge = await self._github.start_device()
        now = self._now()
        async with self._uow_factory() as uow:
            await uow.flows_store.create(
                DeviceFlow(
                    hashlib.sha256(challenge.device_code.encode()).hexdigest(),
                    repository,
                    now + timedelta(seconds=challenge.expires_in),
                    challenge.interval,
                    now + timedelta(seconds=challenge.interval),
                )
            )
            await uow.commit()
        return challenge

    async def poll(self, device_code: str) -> DeviceOutcome:
        code_hash = hashlib.sha256(device_code.encode()).hexdigest()
        now = self._now()
        async with self._uow_factory() as uow:
            flow = await uow.flows_store.reserve(code_hash, now)
            if flow is not None:
                await uow.commit()
        if flow is None:
            return DeviceOutcome("expired")

        result = await self._github.poll_device(device_code)
        if result.status == "pending":
            return DeviceOutcome("pending", retry_after=flow.interval)
        if result.status == "slow_down":
            interval = max(flow.interval + 5, result.interval or 0)
            async with self._uow_factory() as uow:
                await uow.flows_store.slow_down(code_hash, interval, self._now())
                await uow.commit()
            return DeviceOutcome("pending", retry_after=interval)

        async with self._uow_factory() as uow:
            completed = await uow.flows_store.complete(code_hash, self._now())
            if completed:
                await uow.commit()
        if not completed:
            return DeviceOutcome("expired")
        if result.status in ("denied", "expired"):
            return DeviceOutcome(result.status)
        if result.access_token is None:
            raise ValueError("GitHub authorized device flow without an access token")
        user = await self._profile.get_user(result.access_token)
        await self._linker.execute(result.access_token, expected_user_id=user.id)
        token = await self._tokens.issue(user.id, flow.repository)
        return DeviceOutcome("authorized", token)
