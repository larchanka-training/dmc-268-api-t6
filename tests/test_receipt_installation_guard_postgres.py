"""Receipt origin survives current-target lookup, CI observation and enqueue races."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    CiProviderCheckpoint,
    CiSnapshot,
    DetermineCiEligibility,
    EligibilityReason,
)
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestState,
)
from app.modules.reviews.application.trigger_from_delivery import (
    CiTriggerEvent,
    TriggerFromDelivery,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.webhook_run_targets import SqlAlchemyRunTriggerUnitOfWork
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from tests.portal_postgres import INSTALL_A, PR_OPEN, REPO_A, Env, portal_schema
from tests.test_app_reinstall_postgres import FreshGitHub, apply, seed_archive
from tests.test_effective_review_eligibility_postgres import Publisher

_HEAD = "a" * 40
_NOW = datetime.now(UTC)


@pytest.fixture
def receipt_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def prepare(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE repositories SET wait_for_ci='always' WHERE id=:id"), {"id": REPO_A}
        )
        await session.execute(
            text(
                "UPDATE code_changes SET head_sha=:head,ai_review_labeled=true, "
                "ai_review_labeled_at=:now,head_first_seen_at=:now, "
                'ci_status=\'{"keep":"history"}\' WHERE id=:id'
            ),
            {"id": PR_OPEN, "head": _HEAD, "now": _NOW},
        )


async def rebind(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_archive(factory)
    await apply(factory, FreshGitHub())
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE repositories SET enabled=true WHERE id=:id"), {"id": REPO_A}
        )


def event() -> PullRequestEvent:
    return PullRequestEvent(
        action="labeled",
        installation_external_id=17,
        repository_external_id=101,
        external_id=901,
        number=1,
        title="PR",
        description=None,
        author_login="octocat",
        web_url="https://github.test/octo/a/pull/1",
        source_branch="feature",
        target_branch="main",
        base_sha="b" * 40,
        head_sha=_HEAD,
        state=PullRequestState.OPEN,
        provider_updated_at=_NOW,
    )


@dataclass
class Ci:
    factory: async_sessionmaker[AsyncSession]
    rebind_during_read: bool = False
    calls: list[int] = field(default_factory=list)

    async def get_current_head_ci(
        self,
        installation_external_id: int,
        repository_full_name: str,
        head_sha: str,
        *,
        checkpoint: CiProviderCheckpoint,
    ) -> CiSnapshot:
        async with self.factory.begin() as session:
            await session.execute(
                text("SELECT id FROM provider_installations ORDER BY external_id FOR UPDATE NOWAIT")
            )
        self.calls.append(installation_external_id)
        if self.rebind_during_read:
            self.rebind_during_read = False
            await rebind(self.factory)
        return CiSnapshot(_HEAD, (), "success", 1)


@dataclass
class RebindingEnqueuer:
    real: TryEnqueueWebhookRun
    factory: async_sessionmaker[AsyncSession]
    pending_rebind: bool = True
    wait_mode: str = "always"
    origins: list[int | None] = field(default_factory=list)

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> EnqueueResult:
        self.origins.append(expected_installation_external_id)
        if self.pending_rebind:
            self.pending_rebind = False
            await rebind(self.factory)
            async with self.factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET wait_for_ci=:mode WHERE id=:id"),
                    {"mode": self.wait_mode, "id": REPO_A},
                )
        if expected_installation_external_id is None:
            return await self.real.execute(code_change_id, expected_head_sha)
        return await self.real.execute(
            code_change_id,
            expected_head_sha,
            expected_installation_external_id=expected_installation_external_id,
        )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pr", "label", "ci"])
@pytest.mark.parametrize("wait_mode,fresh_ci_calls", [("always", [29]), ("never", [])])
def test_source_receipt_rebind_between_target_read_and_enqueue_never_uses_new_installation(
    receipt_env: Env,
    kind: str,
    wait_mode: str,
    fresh_ci_calls: list[int],
) -> None:
    async def exercise() -> None:
        engine = receipt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ci, publisher = Ci(factory), Publisher()
        try:
            await prepare(factory)
            enqueue = RebindingEnqueuer(
                TryEnqueueWebhookRun(
                    eligibility=DetermineCiEligibility(
                        candidates=SqlAlchemyEligibilityCandidateStore(factory),
                        ci=ci,
                        own_app_id=42,
                    ),
                    uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                    publisher=publisher,
                ),
                factory,
                wait_mode=wait_mode,
            )
            trigger = TriggerFromDelivery(
                uow_factory=lambda: SqlAlchemyRunTriggerUnitOfWork(factory), enqueuer=enqueue
            )

            async def dispatch(installation_id: int) -> str:
                source = replace(event(), installation_external_id=installation_id)
                if kind == "ci":
                    return await trigger.on_ci(
                        CiTriggerEvent(installation_id, 101, _HEAD, "status")
                    )
                if kind == "label":
                    return await trigger.on_label(PullRequestLabelEvent(source, "ai-review"))
                return await trigger.on_pr(source)

            outcome = await dispatch(17)
            assert "ineligible (stale_state)" in outcome
            assert enqueue.origins == [17]
            assert ci.calls == [] and publisher.messages == []
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
            assert "enqueued run=" in await dispatch(29)
            assert enqueue.origins == [17, 29] and ci.calls == fresh_ci_calls
            assert len(publisher.messages) == 1
            assert publisher.messages[0].installation_id == 29
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 5
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_source_ci_observation_that_rebinds_during_read_cannot_enqueue_target_run(
    receipt_env: Env,
) -> None:
    async def exercise() -> None:
        engine = receipt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        ci, publisher = Ci(factory, rebind_during_read=True), Publisher()
        try:
            await prepare(factory)
            trigger = TriggerFromDelivery(
                uow_factory=lambda: SqlAlchemyRunTriggerUnitOfWork(factory),
                enqueuer=TryEnqueueWebhookRun(
                    eligibility=DetermineCiEligibility(
                        candidates=SqlAlchemyEligibilityCandidateStore(factory),
                        ci=ci,
                        own_app_id=42,
                    ),
                    uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                    publisher=publisher,
                ),
            )
            assert "ineligible (stale_state)" in await trigger.on_pr(event())
            assert ci.calls == [17] and publisher.messages == []
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
            assert "enqueued run=" in await trigger.on_pr(
                replace(event(), installation_external_id=29)
            )
            assert ci.calls == [17, 29] and publisher.messages[0].installation_id == 29
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pr", "ci"])
@pytest.mark.parametrize("retired", [False, True])
def test_retired_trigger_targets_do_not_read_or_write_history_as_active(
    receipt_env: Env,
    kind: str,
    retired: bool,
) -> None:
    async def exercise() -> None:
        engine = receipt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory)
            if retired:
                async with factory.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                            "WHERE id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
            async with SqlAlchemyRunTriggerUnitOfWork(factory) as uow:
                if kind == "pr":
                    target = await uow.targets.for_pr(event())
                    assert (target is None) is retired
                    if target is not None:
                        assert target.code_change_id == PR_OPEN
                else:
                    assert await uow.targets.for_ci(CiTriggerEvent(17, 101, _HEAD, "status")) == (
                        () if retired else (PR_OPEN,)
                    )
                await uow.commit()
            async with factory() as session:
                status = await session.scalar(
                    text("SELECT ci_status FROM code_changes WHERE id=:id"), {"id": PR_OPEN}
                )
                assert status == (
                    {"event": "status"} if not retired and kind == "ci" else {"keep": "history"}
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pr", "ci"])
def test_trigger_target_transaction_holds_source_before_ci_history_write(
    receipt_env: Env, kind: str
) -> None:
    async def exercise() -> None:
        engine = receipt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory)
            async with SqlAlchemyRunTriggerUnitOfWork(factory) as uow:
                if kind == "pr":
                    assert await uow.targets.for_pr(event()) is not None
                else:
                    assert await uow.targets.for_ci(CiTriggerEvent(17, 101, _HEAD, "status")) == (
                        PR_OPEN,
                    )
                async with factory.begin() as competing:
                    with pytest.raises(DBAPIError) as blocked:
                        await competing.execute(
                            text(
                                "SELECT id FROM provider_installations WHERE id=:id FOR UPDATE "
                                "NOWAIT"
                            ),
                            {"id": INSTALL_A},
                        )
                    assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@dataclass
class PermissiveEligibility:
    factory: async_sessionmaker[AsyncSession]
    origins: list[int | None] = field(default_factory=list)

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        self.origins.append(expected_installation_external_id)
        candidate = await SqlAlchemyEligibilityCandidateStore(self.factory).get(code_change_id)
        assert candidate is not None
        # The locked insertion must independently enforce the receipt identity,
        # even if an eligibility adapter returns current target state as eligible.
        return CiEligibility(True, EligibilityReason.ELIGIBLE, candidate.head_sha, candidate)


@pytest.mark.integration
@pytest.mark.parametrize(
    "expected_origin,status,run_count", [(17, "stale", 4), (29, "enqueued", 5)]
)
def test_final_locked_enqueue_independently_refuses_wrong_receipt_origin(
    receipt_env: Env,
    expected_origin: int,
    status: str,
    run_count: int,
) -> None:
    async def exercise() -> None:
        engine = receipt_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        eligibility, publisher = PermissiveEligibility(factory), Publisher()
        try:
            await prepare(factory)
            await rebind(factory)
            enqueue = TryEnqueueWebhookRun(
                eligibility=eligibility,
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(factory),
                publisher=publisher,
            )
            result = await enqueue.execute(
                PR_OPEN, _HEAD, expected_installation_external_id=expected_origin
            )
            assert result.status.value == status
            if expected_origin == 17:
                assert result.reason == "state_changed"
            assert eligibility.origins == [expected_origin]
            assert len(publisher.messages) == (0 if expected_origin == 17 else 1)
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == run_count
        finally:
            await engine.dispose()

    asyncio.run(exercise())
