"""GitLab lifecycle never produces GitHub checks; immutable origins stay provider-neutral."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from app.modules.reviews.application.check_runs import (
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.handle_review_run import (
    ClaimedAttempt,
    DeliveryOutcome,
    HandleReviewRun,
    update_check_run,
)
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.infrastructure.github_review_publication import GitHubCheckRunGateway
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
)
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import NOW, Rig, rig, seed_run
from tests.test_gitlab_rerun_postgres import seed_change, seed_github

check_database = projection.authority_database
pytestmark = pytest.mark.integration


@dataclass
class RecordingChecks:
    fixture: Rig
    calls: list[tuple[CheckRunTarget, CheckRunView]] = field(default_factory=list)

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        await checkpoint()
        # The same bounded pool can be acquired only after report/checkpoint UOW closes.
        async with self.fixture.sessions() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        self.calls.append((target, view))


class NoRetry:
    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        raise AssertionError("a terminal delivery cannot retry")


async def no_pipeline(claimed: ClaimedAttempt) -> bool:
    raise AssertionError("terminal redelivery cannot execute analysis")


def test_gitlab_sql_report_is_absent_for_an_active_terminal_origin(
    check_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(check_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            run = await seed_run(fixture, target, change, state="succeeded", attempt=1)
            async with fixture.sessions() as session:
                store = SqlAlchemyRunLifecycleStore(session)
                assert await store.provider_origin_active(run) is True
                assert await store.check_run_report(run) is None

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_gitlab_terminal_redelivery_never_invokes_configured_github_gateway(
    check_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(check_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            run = await seed_run(fixture, target, change, state="succeeded", attempt=1)
            checks = RecordingChecks(fixture)
            outcome = await HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                pipeline=no_pipeline,
                retry_queue=NoRetry(),
                check_runs=checks,
                worker_id="check-worker",
                now=lambda: NOW,
            ).execute(run)
            assert outcome is DeliveryOutcome.ACK
            assert checks.calls == []

    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize(
    ("scenario", "expected_state", "expected_error", "expected_attempt"),
    [
        ("claim", "running", None, 1),
        ("skip", "skipped", "rule_not_matched", 0),
        ("retry", "queued", None, 1),
        ("fail", "failed", "budget_exceeded", 1),
        ("user", "cancelled", "cancelled_by_user", 0),
        ("superseded", "cancelled", "superseded", 0),
        ("revoke", "cancelled", "installation_revoked", 0),
    ],
)
def test_gitlab_public_worker_lifecycle_keeps_github_gateway_idle(
    check_database: tuple[str, str, UUID],
    scenario: str,
    expected_state: str,
    expected_error: str | None,
    expected_attempt: int,
) -> None:
    async def exercise() -> None:
        async with rig(check_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            run = await seed_run(fixture, target, change, head="d" * 40)
            checks = RecordingChecks(fixture)
            retries: list[PendingRunMessage] = []

            class Retry:
                async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
                    assert delay_key == "30s"
                    retries.append(message)

            class Selection:
                async def matches(self, run_id: UUID) -> bool:
                    return scenario != "skip"

            async def pipeline(claimed: ClaimedAttempt) -> bool:
                if scenario in {"retry", "fail"}:
                    raise RunFailure(
                        "llm_timeout" if scenario == "retry" else "budget_exceeded", "test"
                    )
                assert scenario == "claim"
                return True

            if scenario in {"user", "superseded"}:
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE runs SET cancel_requested=:cancel,head_sha=:head WHERE id=:id"
                        ),
                        {
                            "cancel": scenario == "user",
                            "head": "c" * 40 if scenario == "superseded" else "d" * 40,
                            "id": run,
                        },
                    )
            elif scenario == "revoke":
                await fixture.lifecycle.revoke_installation(
                    installation_id=target.origin_provider_installation_id
                )
            outcome = await HandleReviewRun(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                pipeline=pipeline,
                retry_queue=Retry(),
                check_runs=checks,
                selection=Selection(),
                worker_id="check-worker",
                now=lambda: NOW,
            ).execute(run)
            assert outcome is DeliveryOutcome.ACK
            assert checks.calls == []
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text("SELECT state,error_code,attempt FROM runs WHERE id=:id"), {"id": run}
                    )
                ).one()
                assert tuple(row) == (expected_state, expected_error, expected_attempt)
                active = await SqlAlchemyRunLifecycleStore(session).provider_origin_active(run)
                assert active is (scenario != "revoke")
            assert len(retries) == (1 if scenario == "retry" else 0)
            if retries:
                assert (
                    retries[0].provider,
                    retries[0].installation_id,
                    retries[0].repository_id,
                    retries[0].workspace_id,
                ) == ("gitlab", 1, target.repository_id, fixture.workspace)

    asyncio.run(asyncio.wait_for(exercise(), 30))


@pytest.mark.parametrize("lifecycle", ["active", "revoke", "rebind"])
def test_gitlab_origin_never_adopts_overlapping_github_installation_or_sends_http(
    check_database: tuple[str, str, UUID], lifecycle: str
) -> None:
    async def scenario() -> None:
        async with rig(check_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            run = await seed_run(fixture, target, change, state="succeeded", attempt=1)
            # Each fresh schema allocates GitLab compatibility ID1; GitHub owns ID1 too.
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT external_id FROM provider_installations WHERE id=:id"),
                        {"id": target.origin_provider_installation_id},
                    )
                    == 1
                )
            github, repository = await seed_github(
                fixture, target.repository_id, fixture.workspace, 1
            )
            if lifecycle != "active":
                await fixture.lifecycle.revoke_installation(
                    installation_id=target.origin_provider_installation_id
                )
            if lifecycle == "rebind":
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                        {"repo": repository, "id": change},
                    )
            requests: list[str] = []
            tokens: list[int] = []
            urls: list[UUID] = []

            class Tokens:
                async def get_installation_access_token(self, installation_id: int) -> str:
                    tokens.append(installation_id)
                    return "synthetic-check-token"

            def reject(request: httpx.Request) -> httpx.Response:
                requests.append(request.method + " " + request.url.path)
                raise AssertionError("GitLab must never reach GitHub HTTP")

            def run_url(run_id: UUID) -> None:
                urls.append(run_id)

            async with httpx.AsyncClient(
                base_url="https://github.test", transport=httpx.MockTransport(reject)
            ) as client:
                gateway = GitHubCheckRunGateway(client=client, token_provider=Tokens())
                assert (
                    await HandleReviewRun(
                        uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                        pipeline=no_pipeline,
                        retry_queue=NoRetry(),
                        check_runs=gateway,
                        worker_id="check-worker",
                        now=lambda: NOW,
                        run_url=run_url,
                    ).execute(run)
                    is DeliveryOutcome.ACK
                )
            assert requests == []
            assert tokens == []
            assert urls == []
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT origin_provider_installation_id FROM runs WHERE id=:id"),
                        {"id": run},
                    )
                    == target.origin_provider_installation_id
                )
                assert await SqlAlchemyRunLifecycleStore(session).check_run_report(run) is None
                assert github != target.origin_provider_installation_id

    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize("state", ["running", "succeeded"])
def test_github_sql_report_and_public_updater_preserve_trusted_origin_and_view(
    check_database: tuple[str, str, UUID], state: str
) -> None:
    async def scenario() -> None:
        async with rig(check_database) as fixture:
            target = await fixture.seed()
            github, repository = await seed_github(
                fixture, target.repository_id, fixture.workspace, 1
            )
            change = await seed_change(fixture, repository)
            run = await seed_run(fixture, target, change, state=state, attempt=1, origin=github)
            async with fixture.sessions() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(run)
                assert report is not None
                assert report.provider == "github"
                assert report.target == CheckRunTarget(1, "team/project", "a" * 40, run)
            checks = RecordingChecks(fixture)
            await update_check_run(
                lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                checks,
                run,
                lambda _: "https://review.example/runs/github",
            )
            assert len(checks.calls) == 1
            actual_target, view = checks.calls[0]
            assert actual_target == CheckRunTarget(1, "team/project", "a" * 40, run)
            assert (view.status, view.conclusion) == (
                ("in_progress", None) if state == "running" else ("completed", "neutral")
            )
            assert view.details_url == "https://review.example/runs/github"

    asyncio.run(asyncio.wait_for(scenario(), 30))
