"""Durable, one-use GitHub device flow reservations."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.agent_device_access import DeviceFlow, DevicePollTooSoon
from app.modules.auth.infrastructure.models import AuthAgentDeviceFlow


class SqlAlchemyDeviceFlowStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, flow: DeviceFlow) -> None:
        await self._session.execute(
            delete(AuthAgentDeviceFlow).where(AuthAgentDeviceFlow.expires_at < func.now())
        )
        self._session.add(
            AuthAgentDeviceFlow(
                code_hash=flow.code_hash,
                repository=flow.repository,
                expires_at=flow.expires_at,
                interval=flow.interval,
                next_poll_at=flow.next_poll_at,
            )
        )
        await self._session.flush()

    async def reserve(self, code_hash: str, at: datetime) -> DeviceFlow | None:
        row = await self._session.scalar(
            select(AuthAgentDeviceFlow)
            .where(AuthAgentDeviceFlow.code_hash == code_hash)
            .with_for_update()
        )
        if row is None or row.expires_at <= at or row.completed_at is not None:
            return None
        if row.next_poll_at > at:
            raise DevicePollTooSoon(max(1, math.ceil((row.next_poll_at - at).total_seconds())))
        flow = DeviceFlow(
            row.code_hash,
            row.repository,
            row.expires_at,
            row.interval,
            row.next_poll_at,
            row.completed_at,
        )
        row.next_poll_at = at + timedelta(seconds=row.interval)
        await self._session.flush()
        return flow

    async def slow_down(self, code_hash: str, interval: int, at: datetime) -> None:
        row = await self._session.scalar(
            select(AuthAgentDeviceFlow)
            .where(AuthAgentDeviceFlow.code_hash == code_hash)
            .with_for_update()
        )
        if row is not None and row.completed_at is None:
            row.interval = interval
            row.next_poll_at = at + timedelta(seconds=row.interval)
            await self._session.flush()

    async def complete(self, code_hash: str, at: datetime) -> None:
        row = await self._session.scalar(
            select(AuthAgentDeviceFlow)
            .where(AuthAgentDeviceFlow.code_hash == code_hash)
            .with_for_update()
        )
        if row is not None and row.completed_at is None:
            row.completed_at = at
            await self._session.flush()


class SqlAlchemyDeviceFlowUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def flows_store(self) -> SqlAlchemyDeviceFlowStore:
        return SqlAlchemyDeviceFlowStore(self.session)
