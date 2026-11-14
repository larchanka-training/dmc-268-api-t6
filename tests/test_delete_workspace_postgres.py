"""PostgreSQL persistence and read-model integration tests for workspace deletion (#550)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.common.infrastructure.db.enums import LedgerKind
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.current_user import SqlAlchemyCurrentUserRepository
from app.modules.billing.infrastructure.models import CreditLedger
from app.modules.reviews.infrastructure.metrics_summary import SqlAlchemyMetricsSummary
from app.modules.workspaces.domain.status import WorkspaceStatus
from app.modules.workspaces.infrastructure.delete_workspace_store import (
    SqlAlchemyDeleteWorkspaceStore,
    SqlAlchemyDeleteWorkspaceUnitOfWork,
)
from app.modules.workspaces.infrastructure.models import (
    GitHubUserWorkspaceAccess,
    Workspace,
)
from tests.portal_postgres import WS_A, WS_B, Env

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration


def test_delete_workspace_postgres_forfeits_balance_and_records_tombstone(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # 1. Seed initial top_up of $45.50 into credit_ledger for WS_A
        async with session_factory.begin() as session:
            session.add(
                CreditLedger(
                    id=uuid4(),
                    workspace_id=WS_A,
                    kind=LedgerKind.TOP_UP,
                    amount_usd=Decimal("45.50"),
                    reason="seed_top_up",
                )
            )

        # 2. Execute forfeit and mark_deleted through real UnitOfWork and Store
        async with SqlAlchemyDeleteWorkspaceUnitOfWork(session_factory) as uow:
            balance = await uow.store.get_balance(WS_A)
            assert balance == Decimal("45.50")
            await uow.store.forfeit_balance(WS_A, balance)
            await uow.store.mark_deleted(WS_A, user_id=42)
            await uow.commit()

        # 3. Verify ledger balance = 0 and adjustment entry
        async with session_factory() as session:
            store = SqlAlchemyDeleteWorkspaceStore(session)
            balance_after = await store.get_balance(WS_A)
            assert balance_after == Decimal("0")

            ledger_rows = (
                await session.scalars(
                    select(CreditLedger)
                    .where(CreditLedger.workspace_id == WS_A)
                    .order_by(CreditLedger.created_at)
                )
            ).all()
            assert len(ledger_rows) == 2
            adjustment = ledger_rows[1]
            assert adjustment.kind == LedgerKind.ADJUSTMENT
            assert adjustment.amount_usd == Decimal("-45.50")
            assert adjustment.reason == "workspace_deleted"

            # 4. Verify tombstone fields on Workspace
            ws = await session.scalar(select(Workspace).where(Workspace.id == WS_A))
            assert ws is not None
            assert ws.status == WorkspaceStatus.DELETED
            assert ws.deleted_at is not None
            assert ws.deleted_by == 42

            # 5. Verify GitHubUserWorkspaceAccess was removed for deleted workspace
            access_rows = (
                await session.scalars(
                    select(GitHubUserWorkspaceAccess).where(
                        GitHubUserWorkspaceAccess.workspace_id == WS_A
                    )
                )
            ).all()
            assert len(access_rows) == 0

        await engine.dispose()

    asyncio.run(exercise())


def test_deleted_workspace_disappears_from_current_user_and_metrics_summary(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A, WS_B))

        # Give distinct names to WS_A and WS_B
        async with session_factory.begin() as session:
            await session.execute(
                text("UPDATE workspaces SET name = 'Alpha Workspace' WHERE id = :id"),
                {"id": WS_A},
            )
            await session.execute(
                text("UPDATE workspaces SET name = 'Beta Workspace' WHERE id = :id"),
                {"id": WS_B},
            )

        current_user_repo = SqlAlchemyCurrentUserRepository(session_factory)
        metrics_repo = SqlAlchemyMetricsSummary(session_factory, scope)
        now = datetime.now(UTC)

        # Before deletion: user 42 has access to both WS_A and WS_B
        user_before = await current_user_repo.get_current_user(scope)
        assert user_before is not None
        assert {w.id for w in user_before.workspaces} == {WS_A, WS_B}

        metrics_read_before = await metrics_repo.read(
            window_start=now - timedelta(days=7),
            window_end=now,
            today_start=now,
            repo=None,
        )
        assert {s.name for s in metrics_read_before.workspaces} == {
            "Alpha Workspace",
            "Beta Workspace",
        }

        # Delete WS_A
        async with SqlAlchemyDeleteWorkspaceUnitOfWork(session_factory) as uow:
            await uow.store.mark_deleted(WS_A, user_id=42)
            await uow.commit()

        # After deletion: WS_A is filtered out from both current_user and metrics_summary
        user_after = await current_user_repo.get_current_user(scope)
        assert user_after is not None
        assert {w.id for w in user_after.workspaces} == {WS_B}

        metrics_read_after = await metrics_repo.read(
            window_start=now - timedelta(days=7),
            window_end=now,
            today_start=now,
            repo=None,
        )
        assert {s.name for s in metrics_read_after.workspaces} == {"Beta Workspace"}

        await engine.dispose()

    asyncio.run(exercise())
