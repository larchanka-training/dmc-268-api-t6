"""Real metadata checker -> shared locked writer; synthetic fixtures, no broker/HTTP."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.eligibility import DetermineGitLabEligibility
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabRequestUnavailable,
)
from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    WebhookRunIdentity,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.gitlab_eligibility_candidates import (
    SqlAlchemyGitLabEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork

# Import the existing migrated synthetic fixture by module: pytest owns its lifetime.
from tests import test_gitlab_projection_postgres as projection_fixtures
from tests.test_gitlab_projection_postgres import (
    NOW,
    Rig,
    rig,
    seed_run,
)

enqueue_database = projection_fixtures.authority_database
pytestmark = pytest.mark.integration
HEAD = "a" * 40


def expected(target: BoundGitLabTarget) -> WebhookRunIdentity:
    return WebhookRunIdentity(
        "gitlab",
        target.origin_provider_installation_id,
        target.workspace_id,
        target.repository_id,
        17,
        "https://gitlab.com",
        1,
    )


async def seed_change(fixture: Rig, target: BoundGitLabTarget) -> UUID:
    change = uuid4()
    async with fixture.sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO code_changes (id,repository_id,external_id,external_number,title,"
                "source_branch,target_branch,base_sha,head_sha,state,web_url) "
                "VALUES (:id,:repo,901,12,'Canonical MR','feature','main',:base,:head,'open',"
                "'https://gitlab.com/team/project/-/merge_requests/12')"
            ),
            {"id": change, "repo": target.repository_id, "base": "b" * 40, "head": HEAD},
        )
    return change


@dataclass
class Publisher:
    fixture: Rig
    target: BoundGitLabTarget
    fail: bool = False
    messages: list[PendingRunMessage] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        # A second session on the SAME pool1 proves the insertion UOW has exited.
        async with self.fixture.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.origin_provider_installation_id,pi.provider,pi.workspace_id,"
                        "r.head_sha,r.base_sha,r.message_published_at FROM runs r "
                        "JOIN provider_installations pi ON pi.id=r.origin_provider_installation_id "
                        "WHERE r.id=:id"
                    ),
                    {"id": message.run_id},
                )
            ).one()
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND backend_type='client backend' "
                        "AND pid<>pg_backend_pid() AND xact_start IS NOT NULL"
                    )
                )
                == 0
            )
            assert tuple(row) == (
                self.target.origin_provider_installation_id,
                "gitlab",
                self.target.workspace_id,
                HEAD,
                "b" * 40,
                None,
            )
        assert (
            message.provider,
            message.repository_external_id,
            message.base_sha,
            message.pr_number,
        ) == ("gitlab", 17, "b" * 40, 12)
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("synthetic confirmation unavailable")


def enqueue(fixture: Rig, publisher: Publisher) -> TryEnqueueWebhookRun:
    return TryEnqueueWebhookRun(
        eligibility=DetermineGitLabEligibility(
            candidates=SqlAlchemyGitLabEligibilityCandidateStore(fixture.sessions)
        ),
        uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(fixture.sessions),
        publisher=publisher,
        now=lambda: NOW,
    )


def test_public_gitlab_enqueue_pool_one_pins_origin_start_and_confirmed_marker(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            publisher = Publisher(fixture, target)
            use_case = enqueue(fixture, publisher)
            result = await asyncio.wait_for(
                use_case.execute(change, HEAD, expected_identity=expected(target)), 5
            )
            assert (result.status.value, result.reason) == ("enqueued", None)
            assert len(publisher.messages) == 1
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert await session.scalar(text("SELECT message_published_at FROM runs")) == NOW
                assert (
                    await session.scalar(text("SELECT ai_review_labeled FROM code_changes"))
                    is False
                )
            repeat = await use_case.execute(change, HEAD, expected_identity=expected(target))
            assert repeat.status.value == "duplicate"
            assert len(publisher.messages) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mutation", "status", "reason"),
    [
        ("UPDATE code_changes SET state='closed'", "ineligible", "closed_pr"),
        ("UPDATE code_changes SET state='merged'", "ineligible", "closed_pr"),
        ("UPDATE code_changes SET head_sha=repeat('d',40)", "ineligible", "stale_head"),
        ("UPDATE repositories SET wait_for_ci='auto'", "ineligible", "unsupported_ci_policy"),
        ("UPDATE repositories SET wait_for_ci='always'", "ineligible", "unsupported_ci_policy"),
        ("UPDATE repositories SET enabled=false", "ineligible", "unknown_pr"),
        ("UPDATE repositories SET installation_available=false", "ineligible", "unknown_pr"),
        (
            "UPDATE provider_installations SET deleted_at=clock_timestamp()",
            "ineligible",
            "unknown_pr",
        ),
        ("UPDATE gitlab_connections SET intake_enabled=false", "ineligible", "unknown_pr"),
        (
            "UPDATE gitlab_connections SET intake_enabled=false,"
            "revoked_at=clock_timestamp(),token_ciphertext=NULL,credential_key_id=NULL",
            "ineligible",
            "unknown_pr",
        ),
        ("DELETE FROM gitlab_project_bindings", "ineligible", "unknown_pr"),
        ("UPDATE rule_versions SET is_active=false", "unconfigured", "missing_rules"),
        (
            "UPDATE repositories SET prompt_version_id=NULL; "
            "UPDATE prompt_versions SET is_active=false",
            "unconfigured",
            "missing_prompt",
        ),
    ],
)
def test_public_gitlab_refusal_does_not_insert_publish_or_notify(
    enqueue_database: tuple[str, str, UUID],
    mutation: str,
    status: str,
    reason: str,
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            async with fixture.sessions.begin() as session:
                for statement in mutation.split("; "):
                    await session.execute(text(statement))
            publisher = Publisher(fixture, target)
            with psycopg.connect(
                enqueue_database[0].replace("postgresql+psycopg://", "postgresql://"),
                autocommit=True,
            ) as observer:
                observer.execute("LISTEN run_updated")
                result = await enqueue(fixture, publisher).execute(
                    change, HEAD, expected_identity=expected(target)
                )
                assert (result.status.value, result.reason) == (status, reason)
                assert list(observer.notifies(timeout=0.02, stop_after=1)) == []
            assert publisher.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(scenario())


def test_concurrent_and_metadata_same_head_create_exactly_one_committed_notice(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            publisher = Publisher(fixture, target)
            use_case = enqueue(fixture, publisher)
            with psycopg.connect(
                enqueue_database[0].replace("postgresql+psycopg://", "postgresql://"),
                autocommit=True,
            ) as observer:
                observer.execute("LISTEN run_updated")
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *[
                            use_case.execute(change, HEAD, expected_identity=expected(target))
                            for _ in range(3)
                        ]
                    ),
                    5,
                )
                assert sorted(result.status.value for result in results) == [
                    "duplicate",
                    "duplicate",
                    "enqueued",
                ]
                notices = list(observer.notifies(timeout=0.05))
                assert len(notices) == 1
                assert json.loads(notices[0].payload) == {
                    "run_id": str(publisher.messages[0].run_id),
                    "workspace_id": str(target.workspace_id),
                    "status": "queued",
                }
            async with fixture.sessions.begin() as session:
                await session.execute(text("UPDATE code_changes SET title='metadata only'"))
            result = await use_case.execute(change, HEAD, expected_identity=expected(target))
            assert result.status.value == "duplicate"
            assert len(publisher.messages) == 1

    asyncio.run(scenario())


@dataclass
class AfterDecision:
    real: DetermineGitLabEligibility
    mutation: Callable[[], Awaitable[None]]

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility:
        result = await self.real.execute(
            code_change_id,
            expected_head_sha,
            expected_installation_external_id=expected_installation_external_id,
        )
        assert result.eligible
        await self.mutation()
        return result


@pytest.mark.parametrize(
    "mutation", ["rotate", "revoke", "unbind", "head", "state", "policy", "disable", "rebind"]
)
def test_authority_or_current_state_changes_after_real_detached_decision_cannot_be_adopted(
    enqueue_database: tuple[str, str, UUID],
    mutation: str,
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)

            async def mutate() -> None:
                if mutation == "rotate":
                    await fixture.lifecycle.rotate_token(
                        installation_id=target.origin_provider_installation_id,
                        token="replacement-authority-token",
                        expected_generation=1,
                    )
                elif mutation == "revoke":
                    await fixture.lifecycle.revoke_installation(
                        installation_id=target.origin_provider_installation_id
                    )
                elif mutation == "rebind":
                    # Adversarial fixture-only repository replacement, NOT a public management API.
                    replacement = await fixture.seed(origin="https://other.gitlab.example")
                    async with fixture.sessions.begin() as session:
                        await session.execute(
                            text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                            {"repo": replacement.repository_id, "id": change},
                        )
                else:
                    statements = {
                        "unbind": "DELETE FROM gitlab_project_bindings",
                        "head": "UPDATE code_changes SET head_sha=repeat('d',40)",
                        "state": "UPDATE code_changes SET state='closed'",
                        "policy": "UPDATE repositories SET wait_for_ci='auto'",
                        "disable": "UPDATE repositories SET enabled=false",
                    }
                    async with fixture.sessions.begin() as session:
                        await session.execute(text(statements[mutation]))

            publisher = Publisher(fixture, target)
            use_case = TryEnqueueWebhookRun(
                eligibility=AfterDecision(
                    DetermineGitLabEligibility(
                        candidates=SqlAlchemyGitLabEligibilityCandidateStore(fixture.sessions)
                    ),
                    mutate,
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(fixture.sessions),
                publisher=publisher,
                now=lambda: NOW,
            )
            result = await asyncio.wait_for(
                use_case.execute(change, HEAD, expected_identity=expected(target)), 5
            )
            assert result.status.value in {"stale", "unconfigured"}
            assert result.reason == (
                "missing_installation"
                if mutation in {"revoke", "unbind", "disable"}
                else "state_changed"
            )
            assert publisher.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_requested", [False, True])
def test_different_head_active_work_defers_until_terminal_and_head_duplicate_stays_blocked(
    enqueue_database: tuple[str, str, UUID],
    cancel_requested: bool,
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            old = await seed_run(fixture, target, change, state="running", head="d" * 40)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET cancel_requested=:cancel WHERE id=:id"),
                    {"cancel": cancel_requested, "id": old},
                )
            publisher = Publisher(fixture, target)
            use_case = enqueue(fixture, publisher)
            result = await use_case.execute(change, HEAD, expected_identity=expected(target))
            assert (result.status.value, result.reason) == (
                "deferred" if cancel_requested else "duplicate",
                "active_run",
            )
            assert publisher.messages == []
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='cancelled',finished_at=:now WHERE id=:id"),
                    {"now": NOW, "id": old},
                )
            assert (
                await use_case.execute(change, HEAD, expected_identity=expected(target))
            ).status.value == "enqueued"
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='succeeded',finished_at=:now WHERE head_sha=:head"),
                    {"now": NOW, "head": HEAD},
                )
            duplicate = await use_case.execute(change, HEAD, expected_identity=expected(target))
            assert (duplicate.status.value, duplicate.reason) == (
                "duplicate",
                "head_already_reviewed",
            )

    asyncio.run(scenario())


def test_failed_confirmation_persists_null_marker_without_second_run(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            publisher = Publisher(fixture, target, fail=True)
            use_case = enqueue(fixture, publisher)
            result = await use_case.execute(change, HEAD, expected_identity=expected(target))
            assert result.status.value == "publication_pending"
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert await session.scalar(text("SELECT message_published_at FROM runs")) is None
            assert (
                await use_case.execute(change, HEAD, expected_identity=expected(target))
            ).status.value == "duplicate"
            assert len(publisher.messages) == 1

    asyncio.run(scenario())


def test_metadata_read_never_materializes_secrets_and_query_failure_is_operational(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            selections: list[str] = []

            def observe(
                connection: Connection,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                if statement.startswith("SELECT"):
                    selections.append(statement.split("FROM", 1)[0])

            event.listen(fixture.engine.sync_engine, "before_cursor_execute", observe)
            try:
                store = SqlAlchemyGitLabEligibilityCandidateStore(fixture.sessions)
                candidate = await store.get(change)
                assert candidate is not None
                assert candidate.identity == expected(target)
                assert (
                    candidate.ai_review_labeled,
                    candidate.ai_review_labeled_at,
                    candidate.head_first_seen_at,
                ) == (None, None, None)
                assert all(
                    not any(
                        secret in query
                        for secret in (
                            "token_ciphertext",
                            "credential_key_id",
                            "webhook_secret_digest",
                        )
                    )
                    for query in selections
                )
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "ALTER TABLE gitlab_connections "
                            "RENAME TO fixture_unavailable_connections"
                        )
                    )
                with pytest.raises(
                    GitLabRequestUnavailable, match="^gitlab_request_unavailable$"
                ) as caught:
                    await store.get(change)
                assert caught.value.__cause__ is None
            finally:
                event.remove(fixture.engine.sync_engine, "before_cursor_execute", observe)

    asyncio.run(scenario())


@pytest.mark.parametrize("other_workspace", [False, True])
def test_exact_instance_and_workspace_expected_identity_never_crosses_numeric_collisions(
    enqueue_database: tuple[str, str, UUID],
    other_workspace: bool,
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            workspace = fixture.workspace
            if other_workspace:
                workspace = uuid4()
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO workspaces (id,name,daily_budget_usd) "
                            "VALUES (:id,'other tenant',7)"
                        ),
                        {"id": workspace},
                    )
            replacement = await fixture.seed(
                origin="https://other.gitlab.example", workspace=workspace
            )
            other_change = await seed_change(fixture, replacement)
            # Same numeric installation in provider=github namespace cannot affect GL.
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,metadata) "
                        "SELECT :id,workspace_id,'github',external_id,'{}'::jsonb "
                        "FROM provider_installations WHERE id=:original"
                    ),
                    {"id": uuid4(), "original": target.origin_provider_installation_id},
                )
            publisher = Publisher(fixture, target)
            use_case = enqueue(fixture, publisher)
            wrong = await use_case.execute(
                change,
                HEAD,
                expected_identity=WebhookRunIdentity(
                    "gitlab",
                    replacement.origin_provider_installation_id,
                    replacement.workspace_id,
                    replacement.repository_id,
                    17,
                    "https://other.gitlab.example",
                    1,
                ),
            )
            assert (wrong.status.value, wrong.reason) == ("stale", "state_changed")
            assert publisher.messages == []
            assert (
                await use_case.execute(change, HEAD, expected_identity=expected(target))
            ).status.value == "enqueued"
            other_publisher = Publisher(fixture, replacement)
            other_use_case = enqueue(fixture, other_publisher)
            result = await other_use_case.execute(
                other_change,
                HEAD,
                expected_identity=WebhookRunIdentity(
                    "gitlab",
                    replacement.origin_provider_installation_id,
                    workspace,
                    replacement.repository_id,
                    17,
                    "https://other.gitlab.example",
                    1,
                ),
            )
            assert result.status.value == "enqueued"
            assert len(other_publisher.messages) == 1

    asyncio.run(scenario())


def test_rebind_while_waiting_on_original_workspace_does_not_adopt_replacement(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            replacement = await fixture.seed(origin="https://other.gitlab.example")
            waiting = asyncio.Event()

            def observe(
                connection: Connection,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                if "FROM workspaces" in statement and "FOR UPDATE" in statement:
                    waiting.set()

            event.listen(fixture.engine.sync_engine, "before_cursor_execute", observe)
            publisher = Publisher(fixture, target)
            pending = None
            try:
                with psycopg.connect(
                    enqueue_database[0].replace("postgresql+psycopg://", "postgresql://")
                ) as external:
                    external.execute(f'SET search_path TO "{enqueue_database[1]}"')
                    external.execute(
                        "SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (fixture.workspace,)
                    )
                    pending = asyncio.create_task(
                        enqueue(fixture, publisher).execute(
                            change, HEAD, expected_identity=expected(target)
                        )
                    )
                    await asyncio.wait_for(waiting.wait(), 3)
                    # Source-first ordering: waiting enqueue has NOT locked CodeChange.
                    external.execute(
                        "SELECT id FROM code_changes WHERE id=%s FOR UPDATE NOWAIT", (change,)
                    )
                    external.execute(
                        "UPDATE code_changes SET repository_id=%s WHERE id=%s",
                        (replacement.repository_id, change),
                    )
                    external.commit()
                result = await asyncio.wait_for(pending, 5)
                assert (result.status.value, result.reason) == (
                    "unconfigured",
                    "missing_installation",
                )
                assert publisher.messages == []
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
            finally:
                event.remove(fixture.engine.sync_engine, "before_cursor_execute", observe)
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending

    asyncio.run(scenario())


def test_gitlab_checker_rejects_current_github_parent_even_with_matching_numeric_ids(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            github = uuid4()
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,metadata) "
                        "SELECT :id,workspace_id,'github',external_id,'{}'::jsonb "
                        "FROM provider_installations WHERE id=:original"
                    ),
                    {"id": github, "original": target.origin_provider_installation_id},
                )
                await session.execute(text("DELETE FROM gitlab_project_bindings"))
                await session.execute(
                    text("UPDATE repositories SET provider_installation_id=:id"), {"id": github}
                )
            publisher = Publisher(fixture, target)
            result = await enqueue(fixture, publisher).execute(
                change, HEAD, expected_identity=expected(target)
            )
            assert (result.status.value, result.reason) == ("ineligible", "unknown_pr")
            assert publisher.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["exit", "cancel", "body_and_exit"])
def test_owned_metadata_session_close_failure_is_safe_and_preserves_cancellation(
    enqueue_database: tuple[str, str, UUID],
    fault: str,
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            if fault == "body_and_exit":
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "ALTER TABLE gitlab_connections "
                            "RENAME TO fixture_unavailable_connections"
                        )
                    )

            class ExitFailureSession(AsyncSession):
                async def close(self) -> None:
                    await super().close()
                    if fault == "cancel":
                        raise asyncio.CancelledError()
                    raise RuntimeError("synthetic-driver-secret-canary")

            sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
                fixture.engine, class_=ExitFailureSession
            )
            checker = DetermineGitLabEligibility(
                candidates=SqlAlchemyGitLabEligibilityCandidateStore(sessions)
            )
            if fault == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await checker.execute(change, HEAD)
            else:
                with pytest.raises(
                    GitLabRequestUnavailable, match="^gitlab_request_unavailable$"
                ) as caught:
                    await checker.execute(change, HEAD)
                assert caught.value.__cause__ is None
                assert "secret-canary" not in str(caught.value)

    asyncio.run(scenario())


def test_pending_gitlab_failed_confirm_replays_same_run_and_marks_only_after_confirm(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            publisher = Publisher(fixture, target, fail=True)
            use_case = enqueue(fixture, publisher)
            result = await use_case.execute(change, HEAD, expected_identity=expected(target))
            assert result.status.value == "publication_pending"
            original = publisher.messages[0]
            publisher.fail = False
            assert await asyncio.wait_for(use_case.replay_pending_publications(), 5) == 1
            assert publisher.messages == [original, original]
            async with fixture.sessions() as session:
                row = (
                    await session.execute(text("SELECT id,message_published_at,attempt FROM runs"))
                ).one()
                assert tuple(row) == (result.run_id, NOW, 0)
            assert await use_case.replay_pending_publications() == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        "rotate",
        "revoke",
        "unbind",
        "disabled",
        "unavailable",
        "intake_off",
        "auto",
        "always",
        "closed",
        "head",
        "cancel",
        "published",
        "null_origin",
        "rebind",
    ],
)
def test_pending_gitlab_replay_refuses_changed_origin_authority_or_canonical_state(
    enqueue_database: tuple[str, str, UUID],
    mutation: str,
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            publisher = Publisher(fixture, target, fail=True)
            use_case = enqueue(fixture, publisher)
            result = await use_case.execute(change, HEAD, expected_identity=expected(target))
            assert result.status.value == "publication_pending"
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
            elif mutation == "rebind":
                workspace = uuid4()
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO workspaces (id,name,daily_budget_usd) VALUES "
                            "(:id,'other',7)"
                        ),
                        {"id": workspace},
                    )
                replacement = await fixture.seed(
                    origin="https://other.gitlab.example", workspace=workspace
                )
                # Historical Run stays A; current CodeChange belongs to B, across tenants/instances.
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                        {"repo": replacement.repository_id, "id": change},
                    )
            else:
                changes = {
                    "unbind": "DELETE FROM gitlab_project_bindings",
                    "disabled": "UPDATE repositories SET enabled=false",
                    "unavailable": "UPDATE repositories SET installation_available=false",
                    "intake_off": "UPDATE gitlab_connections SET intake_enabled=false",
                    "auto": "UPDATE repositories SET wait_for_ci='auto'",
                    "always": "UPDATE repositories SET wait_for_ci='always'",
                    "closed": "UPDATE code_changes SET state='closed'",
                    "head": "UPDATE code_changes SET head_sha=repeat('d',40)",
                    "cancel": "UPDATE runs SET cancel_requested=true",
                    "published": "UPDATE runs SET message_published_at=now()",
                    "null_origin": "UPDATE runs SET origin_provider_installation_id=NULL",
                }
                async with fixture.sessions.begin() as session:
                    await session.execute(text(changes[mutation]))
            publisher.fail = False
            publisher.messages.clear()
            assert await asyncio.wait_for(use_case.replay_pending_publications(), 5) == 0
            assert publisher.messages == []
            async with fixture.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id,state,cancel_requested,error_code,message_published_at "
                            "FROM runs"
                        )
                    )
                ).one()
                assert row[0] == result.run_id
                if mutation in {"rotate", "revoke"}:
                    assert tuple(row[1:4]) == (
                        "cancelled",
                        True,
                        "credential_rotated" if mutation == "rotate" else "installation_revoked",
                    )
                if mutation != "published":
                    assert row[4] is None

    asyncio.run(scenario())


def test_pending_gitlab_inactive_earlier_row_does_not_starve_valid_origin_with_pool_one(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            inactive = await fixture.seed()
            old_change = await seed_change(fixture, inactive)
            old_publisher = Publisher(fixture, inactive, fail=True)
            old_result = await enqueue(fixture, old_publisher).execute(old_change, HEAD)
            valid = await fixture.seed(origin="https://other.gitlab.example")
            change = await seed_change(fixture, valid)
            publisher = Publisher(fixture, valid, fail=True)
            use_case = enqueue(fixture, publisher)
            result = await use_case.execute(change, HEAD)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET intake_enabled=false WHERE "
                        "provider_installation_id=:id"
                    ),
                    {"id": inactive.origin_provider_installation_id},
                )
                await session.execute(
                    text("UPDATE runs SET created_at=:now WHERE id=:id"),
                    {"now": NOW + timedelta(seconds=1), "id": result.run_id},
                )
            selections: list[str] = []

            def observe(
                connection: Connection,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                selections.extend(
                    part.split("FROM", 1)[0] for part in statement.split("SELECT")[1:]
                )

            event.listen(fixture.engine.sync_engine, "before_cursor_execute", observe)
            try:
                publisher.fail = False
                publisher.messages.clear()
                assert await asyncio.wait_for(use_case.replay_pending_publications(limit=1), 5) == 1
                assert [message.run_id for message in publisher.messages] == [result.run_id]
                assert all(
                    not any(
                        secret in selection
                        for secret in (
                            "token_ciphertext",
                            "credential_key_id",
                            "webhook_secret_digest",
                        )
                    )
                    for selection in selections
                )
            finally:
                event.remove(fixture.engine.sync_engine, "before_cursor_execute", observe)
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 2
                assert (
                    await session.scalar(
                        text("SELECT message_published_at FROM runs WHERE id=:id"),
                        {"id": old_result.run_id},
                    )
                    is None
                )

    asyncio.run(scenario())


def test_pending_gitlab_crash_after_confirm_before_mark_redelivers_same_run(
    enqueue_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(enqueue_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)

            class CrashAfterConfirm(Publisher):
                crash: bool = True

                async def publish_confirmed(
                    self,
                    message: PendingRunMessage,
                    *,
                    kind: RunPublicationKind = RunPublicationKind.QUEUED,
                ) -> None:
                    await super().publish_confirmed(message, kind=kind)
                    # External broker has confirmed; process loss prevents separate DB marking.
                    if self.crash:
                        raise asyncio.CancelledError()

            publisher = CrashAfterConfirm(fixture, target)
            use_case = enqueue(fixture, publisher)
            with pytest.raises(asyncio.CancelledError):
                await use_case.execute(change, HEAD)
            original = publisher.messages[0]
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT message_published_at FROM runs")) is None
            publisher.crash = False
            assert await use_case.replay_pending_publications() == 1
            assert publisher.messages == [original, original]
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert await session.scalar(text("SELECT message_published_at FROM runs")) == NOW

    asyncio.run(scenario())
