"""PostgreSQL persistence and read-model integration tests for workspace deletion (#550)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.common.infrastructure.db.enums import LedgerKind
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.current_user import SqlAlchemyCurrentUserRepository
from app.modules.billing.infrastructure.models import CreditLedger
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
)
from app.modules.integrations.gitlab.infrastructure.connection_store import (
    SqlAlchemyGitLabConnectionStore,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.determine_ci_eligibility import (
    CandidateMiss,
    CiWaitMode,
    EligibilityCandidate,
    WebhookRunIdentity,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.infrastructure.metrics_summary import SqlAlchemyMetricsSummary
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
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
from tests.portal_postgres import INSTALL_B, REPO_B, WS_A, WS_B, Env

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
            assert await SqlAlchemyGitLabConnectionStore(session).workspace_exists(WS_A) is True

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
            assert await SqlAlchemyGitLabConnectionStore(session).workspace_exists(WS_A) is False
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


async def _seed_and_delete_ws_b(
    session_factory: async_sessionmaker,
    user_id: int,
) -> None:
    # Seed INSTALL_B with account_external_id and metadata for OAuth linking
    async with session_factory.begin() as session:
        await session.execute(
            update(ProviderInstallation)
            .where(ProviderInstallation.id == INSTALL_B)
            .values(
                account_external_id=123,
                provider_metadata={"account_login": "org-test"},
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
        confirmation_name="W",
    )


def test_oauth_link_after_deletion_does_not_resurrect_workspace(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        user_id = 42
        await _seed_and_delete_ws_b(session_factory, user_id)

        # Same installation.id (reinstall/relink after deletion):
        # rejects and does not resurrect workspace
        installation_snapshot = GitHubInstallation(
            id=18,
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

            assert linked_ws_id is None

            # Assert no access row for deleted workspace
            access_for_deleted = (
                await session.scalars(
                    select(GitHubUserWorkspaceAccess).where(
                        GitHubUserWorkspaceAccess.workspace_id == WS_B
                    )
                )
            ).all()
            assert len(access_for_deleted) == 0

            # Assert user has no access granted
            user_access = (
                await session.scalars(
                    select(GitHubUserWorkspaceAccess).where(
                        GitHubUserWorkspaceAccess.github_user_id == user_id
                    )
                )
            ).all()
            assert WS_B not in [a.workspace_id for a in user_access]

            # Assert ProviderInstallation row and its tombstone are preserved
            inst_row = (
                await session.scalars(
                    select(ProviderInstallation).where(
                        ProviderInstallation.provider == "github",
                        ProviderInstallation.external_id == 18,
                    )
                )
            ).first()
            assert inst_row is not None
            assert inst_row.workspace_id == WS_B
            assert inst_row.deleted_at is not None

            # Assert Repository row remains unchanged and points to INSTALL_B
            repo_row = (
                await session.scalars(
                    select(Repository).where(
                        Repository.id == REPO_B,
                    )
                )
            ).first()
            assert repo_row is not None
            assert repo_row.provider_installation_id == INSTALL_B

        await engine.dispose()

    asyncio.run(exercise())


def test_oauth_link_with_new_installation_id_after_deletion_creates_new_workspace(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        user_id = 42
        await _seed_and_delete_ws_b(session_factory, user_id)

        # Directly link new installation.id with same account_external_id
        # (isolated without case 1 preceding it)
        new_installation_snapshot = GitHubInstallation(
            id=998877,
            account_external_id=123,
            account_login="org-test",
        )
        async with session_factory() as session:
            link_store = SqlAlchemyGitHubInstallationLinkStore(session)
            new_ws_id = await link_store.link(
                user_id=user_id,
                installation=new_installation_snapshot,
            )
            await session.commit()

            assert new_ws_id is not None
            assert new_ws_id != WS_B

            access_for_deleted = (
                await session.scalars(
                    select(GitHubUserWorkspaceAccess).where(
                        GitHubUserWorkspaceAccess.workspace_id == WS_B
                    )
                )
            ).all()
            assert len(access_for_deleted) == 0

            access_for_new = (
                await session.scalars(
                    select(GitHubUserWorkspaceAccess).where(
                        GitHubUserWorkspaceAccess.workspace_id == new_ws_id,
                        GitHubUserWorkspaceAccess.github_user_id == user_id,
                    )
                )
            ).all()
            assert len(access_for_new) == 1

        await engine.dispose()

    asyncio.run(exercise())


def test_lock_gitlab_rejects_deleted_workspace_postgres(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # 1. Mark WS_A deleted
        async with SqlAlchemyDeleteWorkspaceUnitOfWork(session_factory) as uow:
            await uow.store.mark_deleted(WS_A, user_id=42)
            await uow.commit()

        # 2. SqlAlchemyWebhookRunStore._lock_gitlab rejects deleted workspace on Postgres
        async with session_factory() as session:
            webhook_store = SqlAlchemyWebhookRunStore(session)
            target = BoundGitLabTarget(
                origin_provider_installation_id=uuid4(),
                workspace_id=WS_A,
                repository_id=uuid4(),
                instance_origin="https://gitlab.com",
                project_id=123,
                credential_generation=1,
            )
            with patch(
                "app.modules.reviews.infrastructure.webhook_runs.read_gitlab_eligibility_target",
                new=AsyncMock(return_value=target),
            ):
                miss_gl = await webhook_store._lock_gitlab(uuid4())
                assert miss_gl == CandidateMiss.MISSING_INSTALLATION

        await engine.dispose()

    asyncio.run(exercise())


def test_lock_bitbucket_rejects_deleted_workspace_postgres(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # 1. Mark WS_A deleted
        async with SqlAlchemyDeleteWorkspaceUnitOfWork(session_factory) as uow:
            await uow.store.mark_deleted(WS_A, user_id=42)
            await uow.commit()

        # 2. SqlAlchemyWebhookRunStore._lock_bitbucket rejects deleted workspace on Postgres
        async with session_factory() as session:
            webhook_store = SqlAlchemyWebhookRunStore(session)
            identity = WebhookRunIdentity(
                provider="bitbucket",
                origin_provider_installation_id=uuid4(),
                workspace_id=WS_A,
                repository_id=uuid4(),
                repository_external_id=12345,
                repository_uuid=uuid4(),
                credential_generation=1,
                binding_generation=1,
                webhook_generation=1,
            )
            candidate = EligibilityCandidate(
                code_change_id=uuid4(),
                installation_external_id=12345,
                repository_full_name="org/repo",
                head_sha="deadbeef",
                state=PullRequestState.OPEN,
                repository_enabled=True,
                ai_review_labeled=True,
                ai_review_labeled_at=None,
                head_first_seen_at=None,
                wait_for_ci=CiWaitMode.NEVER,
                identity=identity,
            )
            with patch(
                "app.modules.reviews.infrastructure.webhook_runs.read_bitbucket_eligibility_candidate",
                new=AsyncMock(return_value=candidate),
            ):
                miss_bb = await webhook_store._lock_bitbucket(uuid4())
                assert miss_bb == CandidateMiss.MISSING_INSTALLATION

        await engine.dispose()

    asyncio.run(exercise())
