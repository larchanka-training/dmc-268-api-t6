"""PostgreSQL persistence and read-model integration tests for workspace deletion (#550)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.common.infrastructure.db.enums import LedgerKind
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.current_user import SqlAlchemyCurrentUserRepository
from app.modules.billing.infrastructure.models import CreditLedger
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.reviews.infrastructure.metrics_summary import SqlAlchemyMetricsSummary
from app.modules.workspaces.application.delete_workspace import (
    DeleteWorkspace,
    DeleteWorkspaceStore,
)
from app.modules.workspaces.application.link_github_installations import GitHubInstallation
from app.modules.workspaces.domain.status import WorkspaceStatus
from app.modules.workspaces.infrastructure.delete_workspace_store import (
    SqlAlchemyDeleteWorkspaceStore,
    SqlAlchemyDeleteWorkspaceUnitOfWork,
)
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkStore,
)
from app.modules.workspaces.infrastructure.models import (
    GitHubUserWorkspaceAccess,
    Workspace,
)
from tests.delete_workspace_fakes import FakePaymentGateway
from tests.portal_postgres import WS_A, WS_B, Env

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration


class _OwnerOverridingStore(SqlAlchemyDeleteWorkspaceStore):
    async def is_owner(self, workspace_id: UUID, user_id: int) -> bool:
        del workspace_id, user_id
        return True


class _OwnerOverridingUnitOfWork(SqlAlchemyDeleteWorkspaceUnitOfWork):
    @property
    def store(self) -> DeleteWorkspaceStore:
        return _OwnerOverridingStore(self.session)


def test_delete_workspace_postgres_forfeits_balance_and_records_tombstone(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # 1. Seed initial balance of $45.50 into credit_ledger for WS_A
        async with session_factory.begin() as session:
            session.add(
                CreditLedger(
                    id=uuid4(),
                    workspace_id=WS_A,
                    kind=LedgerKind.ADJUSTMENT,
                    amount_usd=Decimal("45.50"),
                    reason="seed_grant",
                )
            )

        # 2. Execute forfeit and mark_deleted through real UnitOfWork and Store
        async with SqlAlchemyDeleteWorkspaceUnitOfWork(session_factory) as uow:
            ws_before = await uow.store.get_workspace_for_deletion(WS_A)
            assert ws_before is not None
            assert ws_before.status == WorkspaceStatus.ACTIVE
            balance = await uow.store.get_balance(WS_A)
            assert balance == Decimal("45.50")
            await uow.store.forfeit_balance(WS_A, balance)
            await uow.store.mark_deleted(WS_A, user_id=42)
            await uow.commit()

        # 3. Verify ledger balance = 0 and adjustment entry
        async with session_factory() as session:
            store = SqlAlchemyDeleteWorkspaceStore(session)
            ws_after = await store.get_workspace_for_deletion(WS_A)
            assert ws_after is not None
            assert ws_after.status == WorkspaceStatus.DELETED
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

        # Delete WS_A directly via status update without removing GitHubUserWorkspaceAccess,
        # isolating and verifying that the status != 'deleted' filter functions independently.
        async with session_factory.begin() as session:
            await session.execute(
                text("UPDATE workspaces SET status = 'deleted', deleted_at = now() WHERE id = :id"),
                {"id": WS_A},
            )

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


def test_delete_workspace_use_case_execute_postgres(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # Seed initial balance of $25.00 for WS_A
        async with session_factory.begin() as session:
            session.add(
                CreditLedger(
                    id=uuid4(),
                    workspace_id=WS_A,
                    kind=LedgerKind.ADJUSTMENT,
                    amount_usd=Decimal("25.00"),
                    reason="seed_grant",
                )
            )

        gateway = FakePaymentGateway()
        use_case = DeleteWorkspace(
            uow_factory=lambda: _OwnerOverridingUnitOfWork(session_factory),
            payment_gateway=gateway,
        )

        await use_case.execute(
            workspace_id=WS_A,
            user_id=42,
            confirmation_name="W",
        )

        assert [c[0] for c in gateway.calls] == [WS_A]

        async with session_factory() as session:
            store = SqlAlchemyDeleteWorkspaceStore(session)
            ws = await store.get_workspace_for_deletion(WS_A)
            assert ws is not None
            assert ws.status == WorkspaceStatus.DELETED
            balance = await store.get_balance(WS_A)
            assert balance == Decimal("0")

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


def test_oauth_link_after_deletion_does_not_resurrect_workspace(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        installation_id = 998877
        user_id = 42

        async with session_factory.begin() as session:
            session.add(
                Workspace(
                    id=WS_B,
                    name="Test Workspace B",
                    status=WorkspaceStatus.ACTIVE,
                    daily_budget_usd=Decimal("0"),
                )
            )
            session.add(
                ProviderInstallation(
                    id=uuid4(),
                    workspace_id=WS_B,
                    provider="github",
                    external_id=installation_id,
                    account_external_id=123,
                    account_login="org-test",
                )
            )
            session.add(
                GitHubUserWorkspaceAccess(
                    github_user_id=user_id,
                    workspace_id=WS_B,
                )
            )

        gateway = FakePaymentGateway()
        use_case = DeleteWorkspace(
            uow_factory=lambda: _OwnerOverridingUnitOfWork(session_factory),
            payment_gateway=gateway,
        )
        await use_case.execute(
            workspace_id=WS_B,
            user_id=user_id,
            confirmation_name="Test Workspace B",
        )

        installation_snapshot = GitHubInstallation(
            id=installation_id,
            account_external_id=123,
            account_login="org-test",
        )
        async with session_factory() as session:
            link_store = SqlAlchemyGitHubInstallationLinkStore(session)
            linked_ws_id = await link_store.link(
                user_id=user_id,
                installation=installation_snapshot,
            )
            await session.commit()

            assert linked_ws_id != WS_B

            access_for_deleted = (
                await session.scalars(
                    select(GitHubUserWorkspaceAccess).where(
                        GitHubUserWorkspaceAccess.workspace_id == WS_B
                    )
                )
            ).all()
            assert len(access_for_deleted) == 0

            inst_deleted = (
                await session.scalars(
                    select(ProviderInstallation).where(ProviderInstallation.workspace_id == WS_B)
                )
            ).first()
            assert inst_deleted is not None
            assert inst_deleted.deleted_at is not None

        await engine.dispose()

    asyncio.run(exercise())
