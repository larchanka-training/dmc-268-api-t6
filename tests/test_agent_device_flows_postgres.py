"""Opt-in PostgreSQL proof that polling intervals and one-use codes persist."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.agent_device_access import DeviceFlow, DevicePollTooSoon
from app.modules.auth.infrastructure.agent_device_flows import SqlAlchemyDeviceFlowUnitOfWork
from app.modules.auth.infrastructure.models import AuthAgentDeviceFlow
from tests.portal_postgres import portal_schema


@pytest.mark.integration
def test_device_flow_reservation_and_completion_are_durable() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        code_hash = hashlib.sha256(b"device-code").hexdigest()

        async def exercise() -> None:
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                await uow.flows_store.create(
                    DeviceFlow(
                        code_hash,
                        "octo/a",
                        now + timedelta(minutes=15),
                        5,
                        now + timedelta(seconds=5),
                    )
                )
                await uow.commit()
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                with pytest.raises(DevicePollTooSoon):
                    await uow.flows_store.reserve(code_hash, now)
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                flow = await uow.flows_store.reserve(code_hash, now + timedelta(seconds=5))
                assert flow is not None and flow.repository == "octo/a"
                await uow.commit()
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                assert await uow.flows_store.complete(code_hash, now + timedelta(seconds=6))
                await uow.commit()
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                assert not await uow.flows_store.complete(code_hash, now + timedelta(seconds=7))
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                assert await uow.flows_store.reserve(code_hash, now + timedelta(seconds=10)) is None
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                await uow.flows_store.create(
                    DeviceFlow(
                        "e" * 64,
                        "octo/a",
                        now - timedelta(seconds=1),
                        5,
                        now - timedelta(seconds=6),
                    )
                )
                await uow.commit()
            async with SqlAlchemyDeviceFlowUnitOfWork(factory) as uow:
                await uow.flows_store.create(
                    DeviceFlow(
                        "f" * 64,
                        "octo/a",
                        now + timedelta(minutes=15),
                        5,
                        now + timedelta(seconds=5),
                    )
                )
                await uow.commit()
            async with factory() as session:
                assert await session.scalar(select(func.count(AuthAgentDeviceFlow.code_hash))) == 2

        try:
            asyncio.run(exercise())
        finally:
            asyncio.run(engine.dispose())
