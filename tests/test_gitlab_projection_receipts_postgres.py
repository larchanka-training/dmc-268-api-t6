"""Persisted GitLab receipt and automatic-run flows on real PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
)

from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabOriginPolicy,
)
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.infrastructure.current_merge_request import (
    HttpGitLabCurrentMergeRequestProvider,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_gitlab_merge_request import (
    GitLabMergeRequestCommand,
    GitLabMergeRequestHandler,
    GitLabMergeRequestHandlingResult,
    GitLabMergeRequestProjectionResult,
    HandleGitLabMergeRequest,
    ProjectGitLabMergeRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    UNSET,
    EnqueueResult,
    UnsetType,
)
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
)
from tests.gitlab_projection_support import (
    NOW,
    Clock,
    ConfirmedPointers,
    assert_no_sql_transaction,
    automatic_enqueuer,
    automatic_response,
    canonical,
    command_for,
    lock_for,
    project,
    receipt_for,
    receiver,
    response,
    rig,
)

pytest_plugins = ("tests.gitlab_projection_support",)
pytestmark = pytest.mark.integration
_TOKEN = "synthetic-authority-PAT-canary"
_ORIGIN = "https://gitlab.com"


def test_real_receipt_claim_is_committed_before_http_and_distinct_samehead_replays_are_idempotent(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            clock, calls = Clock(), []
            data = canonical(target)

            async def handle(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                await assert_no_sql_transaction(authority_database[0])
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM webhook_events "
                                "WHERE provider='gitlab' AND projection_claim_token IS NOT NULL "
                                "AND projection_lease_until IS NOT NULL AND projected_at IS NULL"
                            )
                        )
                        == 1
                    )
                return response(data)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                engine = receiver(fixture, project(fixture, client, lock), clock)
                delivery = receipt_for(target)
                assert await engine.execute(delivery) == InstallationDeliveryDispatchStatus.PENDING
                assert (
                    await engine.execute(delivery) == InstallationDeliveryDispatchStatus.DUPLICATE
                )
                assert await engine.replay_pending() == 1
                async with fixture.sessions() as session:
                    first = (
                        await session.execute(
                            text("SELECT id,head_first_seen_at FROM code_changes")
                        )
                    ).one()
                data["title"] = "Canonical metadata update"
                assert (
                    await engine.execute(receipt_for(target, "another-id"))
                    == InstallationDeliveryDispatchStatus.PENDING
                )
                assert await engine.replay_pending() == 1
                assert await engine.replay_pending() == 0
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id,head_first_seen_at,title,head_sha,base_sha FROM code_changes"
                        )
                    )
                ).one()
                assert tuple(row) == (
                    first.id,
                    first.head_first_seen_at,
                    "Canonical metadata update",
                    "a" * 40,
                    "b" * 40,
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL "
                            "AND projection_claim_token IS NULL"
                        )
                    )
                    == 2
                )
                assert (
                    await session.scalar(
                        text("SELECT delivery_id FROM webhook_events WHERE delivery_id=:id"),
                        {"id": delivery.delivery_id},
                    )
                    == delivery.delivery_id
                )
            assert len(calls) == 2

    asyncio.run(scenario())


def test_valid_unknown_action_receipt_finishes_without_canonical_http(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()

            def forbidden(request: httpx.Request) -> httpx.Response:
                raise AssertionError("irrelevant action must never reach HTTP")

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                engine = receiver(fixture, project(fixture, client, lock), Clock())
                await engine.execute(receipt_for(target, action="approved"))
                assert await engine.replay_pending() == 1
                assert await engine.replay_pending() == 0
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT projected_at IS NOT NULL FROM webhook_events")
                    )
                    is True
                )
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["notready", "generation"])
def test_real_receipt_exhausts_existing_three_attempt_deferral_or_failure_budget(
    authority_database: tuple[str, str, UUID],
    failure: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            clock, calls = Clock(), []

            async def handle(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))
                await assert_no_sql_transaction(authority_database[0])
                data = canonical(target)
                if failure == "notready":
                    data["diff_refs"] = None
                else:
                    await fixture.lifecycle.rotate_token(
                        installation_id=target.origin_provider_installation_id,
                        token="replacement-authority-token",
                    )
                return response(data)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                engine = receiver(fixture, project(fixture, client, lock), clock)
                await engine.execute(receipt_for(target))
                for _ in range(3):
                    handled = await engine.replay_pending()
                    assert handled == (1 if failure == "notready" else 0)
                    clock.value += timedelta(minutes=6)
                assert await engine.replay_pending() == 0
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT projection_attempt_count,projection_deferred_at IS NOT NULL,"
                            "projection_failed_at IS NOT NULL,projection_claim_token,retry_after "
                            "FROM webhook_events"
                        )
                    )
                ).one()
                assert tuple(row) == (3, failure == "notready", failure == "generation", None, None)
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
            assert len(calls) == (9 if failure == "notready" else 3)

    asyncio.run(scenario())


def test_crash_after_projection_before_finalize_reclaims_and_preserves_one_projection(
    authority_database: tuple[str, str, UUID],
) -> None:
    class CrashAfterProjection:
        def __init__(self, handler: GitLabMergeRequestHandler) -> None:
            self.handler = handler

        async def execute(
            self, command: GitLabMergeRequestCommand
        ) -> GitLabMergeRequestProjectionResult:
            await self.handler.execute(command)
            raise asyncio.CancelledError()

    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock = await fixture.seed(), Clock()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(canonical(target)))
            ) as client:
                use_case = project(fixture, client, lock)
                crashing = receiver(fixture, CrashAfterProjection(use_case), clock)
                await crashing.execute(receipt_for(target))
                with pytest.raises(asyncio.CancelledError):
                    await crashing.replay_pending()
                async with fixture.sessions() as session:
                    first = (
                        await session.execute(
                            text("SELECT id,head_first_seen_at FROM code_changes")
                        )
                    ).one()
                    assert (
                        await session.scalar(
                            text(
                                "SELECT projection_claim_token IS NOT NULL AND projected_at IS "
                                "NULL FROM webhook_events"
                            )
                        )
                        is True
                    )
                clock.value += timedelta(minutes=6)
                retrying = receiver(fixture, use_case, clock)
                assert await retrying.replay_pending() == 1
                assert await retrying.replay_pending() == 0
                async with fixture.sessions() as session:
                    assert tuple(
                        (
                            await session.execute(
                                text("SELECT id,head_first_seen_at FROM code_changes")
                            )
                        ).one()
                    ) == tuple(first)
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
                    assert (
                        await session.scalar(
                            text(
                                "SELECT projected_at IS NOT NULL AND projection_claim_token IS "
                                "NULL FROM webhook_events"
                            )
                        )
                        is True
                    )

    asyncio.run(scenario())


def test_revoked_receipt_origin_cannot_adopt_replacement_or_github_numeric_collision(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            old, clock = await fixture.seed(), Clock()

            def forbidden(request: httpx.Request) -> httpx.Response:
                raise AssertionError("retired origin cannot reach HTTP")

            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                engine = receiver(fixture, project(fixture, client, lock), clock)
                await engine.execute(receipt_for(old))
                await fixture.lifecycle.revoke_installation(
                    installation_id=old.origin_provider_installation_id
                )
                replacement = await fixture.seed()
                async with fixture.sessions.begin() as session:
                    number = await session.scalar(
                        text("SELECT external_id FROM provider_installations WHERE id=:id"),
                        {"id": old.origin_provider_installation_id},
                    )
                    github = uuid4()
                    await session.execute(
                        text(
                            "INSERT INTO provider_installations "
                            "(id,workspace_id,provider,external_id,metadata) VALUES "
                            "(:id,:workspace,'github',:number,'{}')"
                        ),
                        {"id": github, "workspace": fixture.workspace, "number": number},
                    )
                assert await engine.replay_pending() == 0
                with pytest.raises(GitLabTargetInactive):
                    await project(fixture, client, lock).execute(
                        replace(command_for(old), origin_provider_installation_id=github)
                    )
                assert (
                    await fixture.reader.resolve_target(
                        GitLabTargetIdentity(replacement.origin_provider_installation_id, 17)
                    )
                    == replacement
                )
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0
                    assert (
                        await session.scalar(
                            text("SELECT projected_at IS NOT NULL FROM webhook_events")
                        )
                        is True
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
def test_final_locked_uow_refuses_authority_changed_after_all_http_checkpoints(
    authority_database: tuple[str, str, UUID],
    operation: str,
) -> None:
    from app.modules.reviews.application.project_gitlab_merge_request import (
        CanonicalGitLabMergeRequest,
        GitLabMergeRequestCheckpoint,
    )

    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(canonical(target)))
            ) as client:
                provider = HttpGitLabCurrentMergeRequestProvider(
                    client=client,
                    authority=fixture.authority,
                    origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
                )

                class ChangedAfterRead:
                    async def get_current(
                        self,
                        command: GitLabMergeRequestCommand,
                        *,
                        target: BoundGitLabTarget,
                        checkpoint: GitLabMergeRequestCheckpoint,
                    ) -> CanonicalGitLabMergeRequest:
                        current = await provider.get_current(
                            command, target=target, checkpoint=checkpoint
                        )
                        if operation == "rotate":
                            await fixture.lifecycle.rotate_token(
                                installation_id=target.origin_provider_installation_id,
                                token="replacement-authority-token",
                            )
                        else:
                            await fixture.lifecycle.revoke_installation(
                                installation_id=target.origin_provider_installation_id
                            )
                        return current

                use_case = ProjectGitLabMergeRequest(
                    authority=fixture.reader,
                    current_provider=ChangedAfterRead(),
                    projection_lock=lock,
                    uow_factory=lambda: SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(
                        fixture.sessions
                    ),
                )
                with pytest.raises(
                    GitLabCredentialGenerationChanged
                    if operation == "rotate"
                    else GitLabTargetInactive
                ):
                    await asyncio.wait_for(use_case.execute(command_for(target)), 4)
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0

    asyncio.run(scenario())


def test_two_origins_same_project_iid_use_independent_http_and_pool_one(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            first = await fixture.seed()
            second = await fixture.seed(origin="https://other.gitlab.example")
            targets = {first.instance_origin: first, second.instance_origin: second}
            visited: set[str] = set()
            both = asyncio.Event()

            async def handle(request: httpx.Request) -> httpx.Response:
                origin = f"{request.url.scheme}://{request.url.host}"
                visited.add(origin)
                if len(visited) == 2:
                    both.set()
                await both.wait()
                return response(
                    canonical(
                        targets[origin],
                        head="a" * 40 if origin == first.instance_origin else "d" * 40,
                    )
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                use_case = project(fixture, client, lock)
                results = await asyncio.wait_for(
                    asyncio.gather(
                        use_case.execute(command_for(first)), use_case.execute(command_for(second))
                    ),
                    5,
                )
                assert results[0].target == first and results[1].target == second
                assert results[0].code_change_id != results[1].code_change_id
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 2

    asyncio.run(scenario())


def test_persisted_head_clock_changes_only_when_actual_canonical_head_changes(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            clock, data = Clock(), canonical(target)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(data))
            ) as client:
                use_case = project(fixture, client, lock, now=clock)
                await use_case.execute(command_for(target))
                clock.value += timedelta(hours=1)
                data["title"] = "Later metadata"
                unchanged = await use_case.execute(command_for(target))
                assert unchanged.head_changed is False
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(text("SELECT head_first_seen_at FROM code_changes"))
                        == NOW
                    )
                clock.value += timedelta(hours=1)
                data = canonical(target, head="d" * 40)
                changed = await use_case.execute(command_for(target))
                assert changed.head_changed is True
                async with fixture.sessions() as session:
                    assert await session.scalar(
                        text("SELECT head_first_seen_at FROM code_changes")
                    ) == NOW + timedelta(hours=2)

    asyncio.run(scenario())


@pytest.mark.parametrize("body_failure", [False, True])
def test_lock_cleanup_failure_is_sanitized_and_preserves_body_failure(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
    body_failure: bool,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            original = AsyncConnection.execute

            async def fail_reset(
                self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if str(statement) == "RESET lock_timeout":
                    raise RuntimeError("RESET-SQL-DSN-PAT-canary")
                return await original(self, statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", fail_reset)
                expected = LookupError if body_failure else GitLabRequestUnavailable
                with pytest.raises(expected) as caught:
                    async with lock.hold(command_for(target)):
                        if body_failure:
                            raise LookupError("body sentinel")
                assert str(caught.value) == (
                    "body sentinel" if body_failure else "gitlab_request_unavailable"
                )
                assert caught.value.__cause__ is None
            async with lock.hold(command_for(target)):
                pass

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["insert", "commit"])
def test_public_projection_database_failure_has_safe_operational_error_and_no_write(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            scalar = AsyncSession.scalar

            async def fail_insert(
                self: AsyncSession, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if str(statement).startswith("INSERT INTO code_changes"):
                    raise RuntimeError("INSERT-SQL-DSN-PAT-canary")
                return await scalar(self, statement, *args, **kwargs)

            async def fail_commit(self: AsyncSession) -> None:
                raise RuntimeError("COMMIT-SQL-DSN-PAT-canary")

            with monkeypatch.context() as patch:
                patch.setattr(
                    AsyncSession,
                    "scalar" if stage == "insert" else "commit",
                    fail_insert if stage == "insert" else fail_commit,
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: response(canonical(target)))
                ) as client:
                    with pytest.raises(
                        GitLabRequestUnavailable, match="^gitlab_request_unavailable$"
                    ) as caught:
                        await project(fixture, client, lock).execute(command_for(target))
                    assert caught.value.__cause__ is None
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0

    asyncio.run(scenario())


def test_lock_timeout_is_reset_on_actual_backend_before_close(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with (
            rig(authority_database) as fixture,
            lock_for(authority_database, timeout=0.05) as lock,
        ):
            target = await fixture.seed()
            original = AsyncConnection.execute
            reset: list[str] = []

            async def observe(
                self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                result = await original(self, statement, *args, **kwargs)
                if str(statement) == "RESET lock_timeout":
                    reset.append((await original(self, text("SHOW lock_timeout"))).scalar_one())
                return result

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", observe)
                async with lock.hold(command_for(target)):
                    pass
            assert reset == ["0"]

    asyncio.run(scenario())


def test_cancelled_lock_waiter_closes_backend_and_later_acquisition_succeeds(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with (
            rig(authority_database) as fixture,
            lock_for(authority_database) as first,
            lock_for(authority_database) as second,
        ):
            target = await fixture.seed()
            command = command_for(target)
            async with first.hold(command):

                async def waiting() -> None:
                    async with second.hold(command):
                        raise AssertionError("blocked waiter must not acquire")

                task = asyncio.create_task(waiting())
                await asyncio.sleep(0.05)
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            async with second.hold(command):
                pass

    asyncio.run(scenario())


def test_lock_connect_failure_is_safe_operational_and_closed_lock_cannot_reopen(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()

            def fail_connect(self: AsyncEngine) -> AsyncConnection:
                raise RuntimeError("CONNECT-DSN-PAT-canary")

            with monkeypatch.context() as patch:
                patch.setattr(AsyncEngine, "connect", fail_connect)
                with pytest.raises(GitLabRequestUnavailable, match="^gitlab_request_unavailable$"):
                    async with lock.hold(command_for(target)):
                        raise AssertionError("failed connect cannot yield")
            await lock.aclose()
            with pytest.raises(GitLabRequestUnavailable):
                async with lock.hold(command_for(target)):
                    raise AssertionError("closed lock cannot acquire")

    asyncio.run(scenario())


def test_stalled_lock_cleanup_is_finite_and_releases_backend(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target = await fixture.seed()
            original = AsyncConnection.execute

            async def stall_reset(
                self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if str(statement) == "RESET lock_timeout":
                    await asyncio.sleep(7)
                return await original(self, statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", stall_reset)
                with pytest.raises(GitLabRequestUnavailable, match="^gitlab_request_unavailable$"):
                    async with asyncio.timeout(6):
                        async with lock.hold(command_for(target)):
                            pass
            async with lock.hold(command_for(target)):
                pass

    asyncio.run(scenario())


def test_automatic_receipts_create_one_canonical_run_across_metadata_and_redelivery(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock = await fixture.seed(), Clock()
            data = canonical(target)
            pointers = ConfirmedPointers([])
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: automatic_response(request, data))
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=automatic_enqueuer(fixture, pointers),
                )
                engine = receiver(fixture, handler, clock)
                original = receipt_for(target)
                assert await engine.execute(original) == InstallationDeliveryDispatchStatus.PENDING
                assert pointers.messages == []  # admission remains receipt-only
                assert await engine.replay_pending() == 1
                data["title"] = "Canonical metadata update"
                assert await engine.execute(receipt_for(target, "metadata", "update")) == (
                    InstallationDeliveryDispatchStatus.PENDING
                )
                assert await engine.replay_pending() == 1
                assert (
                    await engine.execute(original) == InstallationDeliveryDispatchStatus.DUPLICATE
                )
                assert await engine.replay_pending() == 0
            assert len(pointers.messages) == 1
            message = pointers.messages[0]
            assert (message.provider, message.head_sha, message.base_sha) == (
                "gitlab",
                "a" * 40,
                "b" * 40,
            )
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT c.title,c.head_sha,c.base_sha,c.state,"
                            "r.origin_provider_installation_id,"
                            "r.id,r.message_published_at IS NOT NULL FROM code_changes c "
                            "JOIN runs r ON r.code_change_id=c.id"
                        )
                    )
                ).one()
                assert tuple(row) == (
                    "Canonical metadata update",
                    "a" * 40,
                    "b" * 40,
                    "open",
                    target.origin_provider_installation_id,
                    message.run_id,
                    True,
                )
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 1
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL")
                    )
                    == 2
                )

    asyncio.run(scenario())


def test_automatic_receipt_retries_deferred_head_after_old_run_even_if_projection_unchanged(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock = await fixture.seed(), Clock(datetime.now(UTC))
            data, pointers = canonical(target), ConfirmedPointers([])
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: automatic_response(request, data))
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=automatic_enqueuer(fixture, pointers),
                )
                engine = receiver(fixture, handler, clock)
                await engine.execute(receipt_for(target))
                assert await engine.replay_pending() == 1
                old = pointers.messages[0].run_id
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE runs SET state='running' WHERE id=:id"), {"id": old}
                    )
                data["diff_refs"]["head_sha"] = "d" * 40
                await engine.execute(receipt_for(target, "new-head", "update"))
                assert await engine.replay_pending() == 1
                assert len(pointers.messages) == 1
                async with fixture.sessions.begin() as session:
                    assert (
                        await session.scalar(
                            text("SELECT cancel_requested FROM runs WHERE id=:id"), {"id": old}
                        )
                        is True
                    )
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM webhook_events WHERE projected_at IS NULL")
                        )
                        == 1
                    )
                    await session.execute(
                        text("UPDATE runs SET state='cancelled',finished_at=:now WHERE id=:id"),
                        {"id": old, "now": NOW},
                    )
                clock.value += timedelta(seconds=31)
                assert await engine.replay_pending() == 1
                assert len(pointers.messages) == 2
                assert pointers.messages[1].head_sha == "d" * 40
                assert await engine.replay_pending() == 0
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 2
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM webhook_events WHERE projected_at IS NULL")
                        )
                        == 0
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["rotate", "revoke", "rebind"])
def test_automatic_handler_refuses_changed_authority_after_projection_commit_before_enqueue(
    authority_database: tuple[str, str, UUID],
    mutation: str,
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, pointers = await fixture.seed(), ConfirmedPointers([])
            real_enqueue = automatic_enqueuer(fixture, pointers)

            class ChangeAuthority:
                async def execute(
                    self,
                    code_change_id: UUID,
                    expected_head_sha: str,
                    *,
                    expected_identity: WebhookRunIdentity | None = None,
                    triggered_by: str | None | UnsetType = UNSET,
                ) -> EnqueueResult:
                    # Public lock reacquisition proves projection hold has completed.
                    async with lock.hold(command_for(target)):
                        pass
                    async with fixture.sessions() as session:
                        assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 1
                    if mutation == "rotate":
                        await fixture.lifecycle.rotate_token(
                            installation_id=target.origin_provider_installation_id,
                            token="replacement-authority-token",
                            expected_generation=1,
                        )
                    elif mutation == "revoke":
                        await fixture.lifecycle.revoke_installation(
                            installation_id=target.origin_provider_installation_id,
                        )
                    else:
                        # Adversarial fixture-only replacement; no production rebind API.
                        replacement = await fixture.seed(origin="https://other.gitlab.example")
                        async with fixture.sessions.begin() as session:
                            await session.execute(
                                text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                                {"repo": replacement.repository_id, "id": code_change_id},
                            )
                    return await real_enqueue.execute(
                        code_change_id,
                        expected_head_sha,
                        expected_identity=expected_identity,
                    )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: automatic_response(request, canonical(target))
                )
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=ChangeAuthority(),
                )
                result = await asyncio.wait_for(handler.execute(command_for(target)), 5)
            assert result.projection.target == target
            assert result.enqueue is not None
            assert result.enqueue.status.value in {"ineligible", "stale"}
            assert pointers.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(scenario())


def test_automatic_receipt_crash_after_run_commit_reclaims_without_duplicate_run(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(authority_database) as fixture, lock_for(authority_database) as lock:
            target, clock, pointers = await fixture.seed(), Clock(), ConfirmedPointers([])
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: automatic_response(request, canonical(target))
                )
            ) as client:
                handler = HandleGitLabMergeRequest(
                    projector=project(fixture, client, lock),
                    enqueuer=automatic_enqueuer(fixture, pointers),
                )

                class CrashAfterRun:
                    async def execute(
                        self, command: GitLabMergeRequestCommand
                    ) -> GitLabMergeRequestHandlingResult:
                        await handler.execute(command)
                        raise asyncio.CancelledError()

                crashing = receiver(fixture, CrashAfterRun(), clock)
                await crashing.execute(receipt_for(target))
                with pytest.raises(asyncio.CancelledError):
                    await crashing.replay_pending()
                assert len(pointers.messages) == 1
                clock.value += timedelta(minutes=6)
                assert await receiver(fixture, handler, clock).replay_pending() == 1
                assert len(pointers.messages) == 1
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL"
                            )
                        )
                        == 1
                    )

    asyncio.run(scenario())
