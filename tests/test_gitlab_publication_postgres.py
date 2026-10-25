"""Real supported connection, persisted publishing transition and installed HTTP ports.

Synthetic enabled fixtures and MockTransport are component evidence, not activation/TLS.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
from app.modules.reviews.application.publish_run_review import PublishRunReview
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.gitlab_publication_resources import (
    GitLabInstalledPublicationResources,
)
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
)
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import Rig, rig, seed_run
from tests.test_gitlab_rerun_postgres import seed_change
from tests.test_gitlab_run_http import Stream
from tests.test_publish_run_review import Trace

publication_database = projection.authority_database
pytestmark = pytest.mark.integration


class Queue:
    def __init__(self) -> None:
        self.pointers: list[ReviewPublishPointer] = []

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        self.pointers.append(pointer)


async def publishing(
    fixture: Rig,
    *,
    origin: str = "https://gitlab.com",
    workspace: UUID | None = None,
    replacement: bool = False,
) -> ReviewPublishPointer:
    target = await fixture.seed(origin=origin, workspace=workspace)
    if replacement:
        await fixture.lifecycle.rotate_token(
            installation_id=target.origin_provider_installation_id,
            token="replacement-authority-token",
            expected_generation=1,
        )
    change = await seed_change(fixture, target.repository_id)
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE code_changes SET head_sha=:head WHERE id=:id"),
            {"head": "a" * 40, "id": change},
        )
    run = await seed_run(fixture, target, change, state="running", attempt=1)
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE runs SET worker_id='publication' WHERE id=:id"), {"id": run}
        )
    queue = Queue()
    assert await StoreReviewOutput(
        lambda: SqlAlchemyReviewOutputUnitOfWork(fixture.sessions),
        queue,
        worker_id="publication",
    ).execute(
        run,
        {
            "findings": [],
            "summary": {
                "problem": "No issue found.",
                "done_well": "The code is clear.",
                "effort": "none",
            },
        },
    )
    return queue.pointers[0]


def test_installed_publication_uses_real_trusted_context_and_pool_one(
    publication_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            calls: list[tuple[str, str, str]] = []

            async def external(request: httpx.Request) -> httpx.Response:
                # Sampled boundary: same sole pool lease and locks are available before wire.
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": pointer.run_id},
                    )
                calls.append((request.method, str(request.url), request.headers["PRIVATE-TOKEN"]))
                if request.url.path.endswith("/versions"):
                    body = (
                        b'[{"head_commit_sha":"'
                        + b"a" * 40
                        + b'","base_commit_sha":"'
                        + b"b" * 40
                        + b'","start_commit_sha":"'
                        + b"b" * 40
                        + b'"}]'
                    )
                    return httpx.Response(
                        200, headers={"Content-Type": "application/json"}, stream=Stream((body,))
                    )
                body = b"[]" if request.method == "GET" else b'{"notes":[{"id":701}]}'
                return httpx.Response(
                    200 if request.method == "GET" else 201,
                    headers={"Content-Type": "application/json"},
                    stream=Stream((body,)),
                )

            resources = GitLabInstalledPublicationResources(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                transport_factory=lambda: httpx.MockTransport(external),
            )
            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=Trace(),
                gitlab_publications=resources,
                sleep=lambda _: asyncio.sleep(0),
            ).execute(pointer)
            async with fixture.sessions() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
            assert context is not None and context.state == "succeeded"
            assert context.workspace_id == fixture.workspace
            assert context.vcs_input is not None
            assert calls == [
                (
                    "GET",
                    "https://gitlab.com/api/v4/projects/17/merge_requests/12/versions",
                    "synthetic-authority-PAT-canary",
                ),
                (
                    "GET",
                    "https://gitlab.com/api/v4/projects/17/merge_requests/12/"
                    "discussions?page=1&per_page=100",
                    "synthetic-authority-PAT-canary",
                ),
                (
                    "GET",
                    "https://gitlab.com/api/v4/projects/17/merge_requests/12/versions",
                    "synthetic-authority-PAT-canary",
                ),
                (
                    "POST",
                    "https://gitlab.com/api/v4/projects/17/merge_requests/12/discussions",
                    "synthetic-authority-PAT-canary",
                ),
            ]

    asyncio.run(asyncio.wait_for(scenario(), 25))


def wire(status: int, payload: object, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json", **headers},
        stream=Stream((json.dumps(payload).encode(),)),
    )


def versions() -> list[dict[str, str]]:
    return [
        {"head_commit_sha": "a" * 40, "base_commit_sha": "b" * 40, "start_commit_sha": "b" * 40}
    ]


def installed(fixture: Rig, external: Any) -> GitLabInstalledPublicationResources:
    return GitLabInstalledPublicationResources(
        session_factory=fixture.sessions,
        authority=fixture.authority,
        origin_policy=GitLabOriginPolicy(
            frozenset({"https://gitlab.com", "https://other.gitlab.example"})
        ),
        transport_factory=lambda: httpx.MockTransport(external),
    )


@pytest.mark.parametrize(
    "mutation,at,expected",
    [
        ("rotate", "page", "credential_rotated"),
        ("revoke", "post", "installation_revoked"),
        ("cancel", "post", "cancelled_by_user"),
        ("closed", "post", "pr_closed"),
        ("head", "post", "superseded"),
        ("body", "page", None),
        ("finding", "page", None),
        ("route", "page", None),
        ("rotate", "retry", "credential_rotated"),
    ],
)
def test_current_publication_authority_between_requests_and_retries(
    publication_database: tuple[str, str, UUID],
    mutation: str,
    at: str,
    expected: str | None,
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions() as session:
                initial = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
            assert initial is not None and initial.vcs_input is not None
            code_change = initial.vcs_input.code_change_id
            origin = initial.vcs_input.locator.origin_provider_installation_id
            assert origin is not None
            calls: list[str] = []
            pauses: list[float] = []

            async def change() -> None:
                if mutation == "rotate":
                    await fixture.lifecycle.rotate_token(
                        installation_id=origin,
                        token="replacement-authority-token",
                        expected_generation=1,
                    )
                elif mutation == "revoke":
                    await fixture.lifecycle.revoke_installation(installation_id=origin)
                else:
                    async with fixture.sessions.begin() as session:
                        statement = {
                            "cancel": "UPDATE runs SET cancel_requested=true WHERE id=:id",
                            "closed": "UPDATE code_changes SET state='closed' WHERE id=:id",
                            "head": "UPDATE code_changes SET head_sha=:value WHERE id=:id",
                            "body": "UPDATE runs SET review_body='newer stored body' WHERE id=:id",
                            "route": "UPDATE code_changes SET external_number=19 WHERE id=:id",
                            "finding": (
                                "INSERT INTO findings (id,run_id,file_path,line_start,"
                                "severity,confidence,"
                                "category,title,body,published,inline_comment) VALUES "
                                "(:finding,:id,'x.py',1,'high',0.9,'correctness','New finding',"
                                "'New payload',false,true)"
                            ),
                        }[mutation]
                        await session.execute(
                            text(statement),
                            {
                                "id": code_change
                                if mutation in {"closed", "head", "route"}
                                else pointer.run_id,
                                "value": "c" * 40,
                                "finding": uuid4(),
                            },
                        )

            async def external(request: httpx.Request) -> httpx.Response:
                calls.append(request.method + " " + request.url.path)
                if request.url.path.endswith("/versions"):
                    return wire(200, versions())
                if request.method == "GET":
                    if at == "page":
                        await change()
                        return wire(200, [], **{"X-Next-Page": "2"})
                    return wire(200, [])
                if at == "post":
                    await change()
                if at == "retry":
                    return wire(503, {"message": "private-canary"}, **{"Retry-After": "1"})
                return wire(201, {"notes": [{"id": 701}]})

            async def pause(seconds: float) -> None:
                pauses.append(seconds)
                await change()

            trace = Trace()
            publisher = PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_publications=installed(fixture, external),
                sleep=pause,
            )
            await publisher.execute(pointer)
            async with fixture.sessions() as session:
                current = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
                published = await session.scalar(
                    text("SELECT count(*) FROM findings WHERE run_id=:id AND published"),
                    {"id": pointer.run_id},
                )
            assert current is not None
            assert (current.state, expected is not None) == (
                "cancelled" if expected else "publishing",
                expected is not None,
            )
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT error_code FROM runs WHERE id=:id"), {"id": pointer.run_id}
                    )
                    == expected
                )
            assert published == 0
            assert len(calls) == (2 if at == "page" else 4)
            assert pauses == ([1.0] if at == "retry" else [])
            assert "private-canary" not in repr(trace.records)
            if expected is not None:
                await publisher.execute(pointer)  # Terminal redelivery never revives rotation.
                assert len(calls) == (2 if at == "page" else 4)

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_factory_keeps_two_tenants_same_numeric_target_on_distinct_origins(
    publication_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            sibling = uuid4()
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'sibling',7)"
                    ),
                    {"id": sibling},
                )
            first = await publishing(fixture)
            second = await publishing(
                fixture, origin="https://other.gitlab.example", workspace=sibling, replacement=True
            )
            seen: list[tuple[str, str]] = []

            async def external(request: httpx.Request) -> httpx.Response:
                seen.append((request.url.host or "", request.headers["PRIVATE-TOKEN"]))
                assert request.url.path.startswith("/api/v4/projects/17/merge_requests/12/")
                if request.url.path.endswith("/versions"):
                    return wire(200, versions())
                return (
                    wire(200, [])
                    if request.method == "GET"
                    else wire(201, {"notes": [{"id": 701}]})
                )

            resources = installed(fixture, external)
            publisher = PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=Trace(),
                gitlab_publications=resources,
            )
            await asyncio.gather(publisher.execute(first), publisher.execute(second))
            assert seen.count(("gitlab.com", "synthetic-authority-PAT-canary")) == 4
            assert seen.count(("other.gitlab.example", "replacement-authority-token")) == 4
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM runs WHERE id IN (:a,:b) AND state='succeeded'"),
                        {"a": first.run_id, "b": second.run_id},
                    )
                    == 2
                )

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("changed", ["body", "hash", "route"])
@pytest.mark.parametrize("terminal", ["complete", "finish"])
def test_locked_final_write_refuses_new_payload_after_last_checkpoint(
    publication_database: tuple[str, str, UUID],
    changed: str,
    terminal: str,
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions() as session:
                expected = await SqlAlchemyRunLifecycleStore(session).publish_context(
                    pointer.run_id
                )
            assert expected is not None and expected.vcs_input is not None
            # Simulate the real gap after a successful detached final checkpoint;
            # call the public SQL terminal port with that original immutable context.
            async with fixture.sessions.begin() as session:
                if changed == "body":
                    await session.execute(
                        text("UPDATE runs SET review_body='newer payload' WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                elif changed == "route":
                    await session.execute(
                        text("UPDATE code_changes SET external_number=19 WHERE id=:id"),
                        {"id": expected.vcs_input.code_change_id},
                    )
                else:
                    await session.execute(
                        text(
                            "INSERT INTO findings (id,run_id,file_path,line_start,"
                            "severity,confidence,"
                            "category,title,body,published,inline_comment) VALUES "
                            "(:finding,:id,'x.py',1,'high',0.9,'correctness','New finding',"
                            "'New payload',false,true)"
                        ),
                        {"finding": uuid4(), "id": pointer.run_id},
                    )
            async with fixture.sessions.begin() as session:
                store = SqlAlchemyRunLifecycleStore(session)
                if terminal == "complete":
                    assert (
                        await store.complete_publication(
                            pointer.run_id,
                            review_id=701,
                            comment_ids=(),
                            findings_hash=expected.findings_hash,
                            moved_to_body_finding_ids=tuple(
                                item.finding_id
                                for item in expected.findings
                                if item.finding_id is not None
                            ),
                            now=datetime.now(UTC),
                            expected_publication=expected,
                        )
                        is None
                    )
                else:
                    assert not await store.finish(
                        pointer.run_id,
                        from_state="publishing",
                        worker_id=None,
                        state="failed",
                        error_code="gitlab_publish_failed",
                        error_message="safe",
                        now=datetime.now(UTC),
                        expected_publication=expected,
                    )
            async with fixture.sessions() as session:
                current = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
                assert current is not None and current.state == "publishing"
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM findings WHERE run_id=:id AND published"),
                        {"id": pointer.run_id},
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM comments c JOIN findings f ON f.id=c.finding_id "
                            "WHERE f.run_id=:id"
                        ),
                        {"id": pointer.run_id},
                    )
                    == 0
                )

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize(
    "facts,reason",
    [("cancel", "cancelled_by_user"), ("head", "superseded"), ("closed", "pr_closed")],
)
def test_locked_final_write_honors_current_cancellation_facts(
    publication_database: tuple[str, str, UUID],
    facts: str,
    reason: str,
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions() as session:
                expected = await SqlAlchemyRunLifecycleStore(session).publish_context(
                    pointer.run_id
                )
            assert expected is not None and expected.vcs_input is not None
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET cancel_requested=true WHERE id=:id"),
                    {"id": pointer.run_id},
                )
                if facts in {"head", "closed"}:
                    await session.execute(
                        text(
                            "UPDATE code_changes SET head_sha=:head, "
                            "state=CAST(:state AS code_change_state) WHERE id=:id"
                        ),
                        {
                            "head": "c" * 40,
                            "state": "closed" if facts == "closed" else "open",
                            "id": expected.vcs_input.code_change_id,
                        },
                    )
            async with fixture.sessions.begin() as session:
                assert (
                    await SqlAlchemyRunLifecycleStore(session).complete_publication(
                        pointer.run_id,
                        review_id=701,
                        comment_ids=(),
                        findings_hash=expected.findings_hash,
                        moved_to_body_finding_ids=(),
                        now=datetime.now(UTC),
                        expected_publication=expected,
                    )
                    == "cancelled"
                )
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT error_code FROM runs WHERE id=:id"), {"id": pointer.run_id}
                    )
                    == reason
                )

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("failures", [1, 4])
def test_real_startup_fault_uses_existing_retry_budget_and_fresh_owned_transports(
    publication_database: tuple[str, str, UUID],
    failures: int,
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            entered: list[int] = []
            closed: list[int] = []
            calls: list[str] = []
            pauses: list[float] = []

            class Startup(httpx.AsyncBaseTransport):
                def __init__(self, index: int) -> None:
                    self.index = index

                async def __aenter__(self) -> Startup:
                    entered.append(self.index)
                    if self.index < failures:
                        raise httpx.ConnectError("startup-secret-canary")
                    return self

                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    calls.append(request.headers["PRIVATE-TOKEN"])
                    if request.url.path.endswith("/versions"):
                        return wire(200, versions())
                    return (
                        wire(200, [])
                        if request.method == "GET"
                        else wire(201, {"notes": [{"id": 701}]})
                    )

                async def aclose(self) -> None:
                    closed.append(self.index)

            def transport() -> Startup:
                return Startup(len(entered))

            async def pause(seconds: float) -> None:
                pauses.append(seconds)

            trace = Trace()
            resources = GitLabInstalledPublicationResources(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                transport_factory=transport,
            )
            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_publications=resources,
                sleep=pause,
            ).execute(pointer)
            assert entered == list(range(2 if failures == 1 else 4))
            assert sorted(closed) == entered
            assert pauses == ([2.0] if failures == 1 else [2.0, 8.0, 30.0])
            assert calls == (["synthetic-authority-PAT-canary"] * 3 if failures == 1 else [])
            assert "startup-secret-canary" not in repr(trace.records)
            async with fixture.sessions() as session:
                assert await session.scalar(
                    text("SELECT state::text FROM runs WHERE id=:id"), {"id": pointer.run_id}
                ) == ("succeeded" if failures == 1 else "failed")

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("primary", ["cancel", "programmer"])
def test_primary_body_fault_survives_response_and_transport_close_faults(
    publication_database: tuple[str, str, UUID],
    primary: str,
) -> None:
    from collections.abc import AsyncIterator

    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            closed: list[str] = []

            class Body(httpx.AsyncByteStream):
                async def __aiter__(self) -> AsyncIterator[bytes]:
                    if primary == "cancel":
                        raise asyncio.CancelledError("primary cancellation")
                    raise RuntimeError("primary programmer fault")
                    yield b""  # pragma: no cover

                async def aclose(self) -> None:
                    closed.append("response")
                    raise ValueError("secondary response close")

            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        200, headers={"Content-Type": "application/json"}, stream=Body()
                    )

                async def aclose(self) -> None:
                    closed.append("transport")
                    raise ValueError("secondary transport close")

            resources = GitLabInstalledPublicationResources(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                transport_factory=Transport,
            )
            with pytest.raises(
                asyncio.CancelledError if primary == "cancel" else RuntimeError, match="primary"
            ):
                await PublishRunReview(
                    uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                    reviews=None,
                    check_runs=None,
                    trace=Trace(),
                    gitlab_publications=resources,
                ).execute(pointer)
            assert closed == ["response", "transport"]
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT state::text FROM runs WHERE id=:id"), {"id": pointer.run_id}
                    )
                    == "publishing"
                )

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_installed_coordinates_fallback_retains_one_transport_and_generation(
    publication_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO findings (id,run_id,file_path,line_start,"
                        "severity,confidence,"
                        "category,title,body,published,inline_comment) VALUES "
                        "(:finding,:id,'x.py',1,'high',0.9,'correctness',"
                        "'Finding','Body',false,true)"
                    ),
                    {"finding": uuid4(), "id": pointer.run_id},
                )
            async with fixture.sessions() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
            assert context is not None
            pointer = ReviewPublishPointer(
                pointer.run_id, pointer.head_sha, context.findings_hash, "COMMENT"
            )
            created: list[httpx.MockTransport] = []
            tokens: list[str] = []
            posts: list[dict[str, Any]] = []

            async def external(request: httpx.Request) -> httpx.Response:
                tokens.append(request.headers["PRIVATE-TOKEN"])
                if request.url.path.endswith("/versions"):
                    return wire(200, versions())
                if request.method == "GET":
                    return wire(200, [])
                payload = json.loads(request.content)
                posts.append(payload)
                if "position" in payload:
                    return wire(400, {"message": "line_code coordinate-private-canary"})
                return wire(201, {"notes": [{"id": 701}]})

            def transport() -> httpx.MockTransport:
                value = httpx.MockTransport(external)
                created.append(value)
                return value

            trace = Trace()
            resources = GitLabInstalledPublicationResources(
                session_factory=fixture.sessions,
                authority=fixture.authority,
                origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                transport_factory=transport,
            )
            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_publications=resources,
            ).execute(pointer)
            assert len(created) == 1 and tokens == ["synthetic-authority-PAT-canary"] * 9
            assert len(posts) == 2 and "position" in posts[0] and "position" not in posts[1]
            assert "`x.py:1`" in posts[1]["body"]
            assert "coordinate-private-canary" not in repr(trace.records)
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT state::text FROM runs WHERE id=:id"), {"id": pointer.run_id}
                    )
                    == "succeeded"
                )
                assert (
                    await session.execute(
                        text("SELECT published,inline_comment FROM findings WHERE run_id=:id"),
                        {"id": pointer.run_id},
                    )
                ).one() == (True, False)

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("fault", ["storage", "codec"])
def test_authority_transient_is_safe_retryable_publication_error_without_source_code(
    publication_database: tuple[str, str, UUID],
    fault: str,
) -> None:
    from app.modules.integrations.gitlab.application.request_credentials import (
        BoundGitLabTarget,
        GitLabCredentialUnavailable,
        GitLabRequestCredential,
        GitLabRequestUnavailable,
        GitLabTargetIdentity,
    )

    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            failures: list[str] = []
            pauses: list[float] = []

            class Authority:
                async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
                    return await fixture.authority.resolve_target(identity)

                async def checkpoint(self, target: BoundGitLabTarget) -> None:
                    await fixture.authority.checkpoint(target)

                async def credential_for(
                    self, target: BoundGitLabTarget
                ) -> GitLabRequestCredential:
                    if not failures:
                        failures.append(fault)
                        if fault == "storage":
                            raise GitLabRequestUnavailable()
                        raise GitLabCredentialUnavailable()
                    return await fixture.authority.credential_for(target)

            async def external(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/versions"):
                    return wire(200, versions())
                return (
                    wire(200, [])
                    if request.method == "GET"
                    else wire(201, {"notes": [{"id": 701}]})
                )

            async def pause(seconds: float) -> None:
                pauses.append(seconds)

            trace = Trace()
            resources = GitLabInstalledPublicationResources(
                session_factory=fixture.sessions,
                authority=Authority(),
                origin_policy=GitLabOriginPolicy(frozenset({"https://gitlab.com"})),
                transport_factory=lambda: httpx.MockTransport(external),
            )
            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_publications=resources,
                sleep=pause,
            ).execute(pointer)
            assert pauses == [2.0]
            assert trace.records[0][2] == {
                "error": {
                    "http_status": None,
                    "message": "GitLab publication authority unavailable",
                }
            }
            assert "diff_fetch_failed" not in repr(trace.records)
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT state::text FROM runs WHERE id=:id"), {"id": pointer.run_id}
                    )
                    == "succeeded"
                )

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize(
    "mutation,final_state,reason",
    [
        ("body", "publishing", None),
        ("route", "publishing", None),
        ("closed", "cancelled", "pr_closed"),
    ],
)
def test_initial_cancellation_is_fenced_across_execute_to_locked_write_gap(
    publication_database: tuple[str, str, UUID],
    mutation: str,
    final_state: str,
    reason: str | None,
) -> None:
    from app.modules.reviews.application.publish_run_review import PublishContext

    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE runs SET cancel_requested=true WHERE id=:id"),
                    {"id": pointer.run_id},
                )
            async with fixture.sessions() as session:
                initial = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
            assert initial is not None and initial.vcs_input is not None
            change = initial.vcs_input.code_change_id

            class RacingStore(SqlAlchemyRunLifecycleStore):
                async def finish(
                    self,
                    run_id: UUID,
                    *,
                    from_state: str,
                    worker_id: str | None,
                    state: str,
                    error_code: str | None,
                    error_message: str | None,
                    now: datetime,
                    expected_publication: PublishContext | None = None,
                ) -> bool:
                    # The public terminal port is the real gap before acquiring the lock.
                    # This UOW has no transaction yet, so the pool1 writer can commit.
                    async with fixture.sessions.begin() as session:
                        statement = {
                            "body": "UPDATE runs SET review_body='newer payload' WHERE id=:id",
                            "route": "UPDATE code_changes SET external_number=19 WHERE id=:id",
                            "closed": "UPDATE code_changes SET state='closed' WHERE id=:id",
                        }[mutation]
                        await session.execute(
                            text(statement), {"id": run_id if mutation == "body" else change}
                        )
                    return await super().finish(
                        run_id,
                        from_state=from_state,
                        worker_id=worker_id,
                        state=state,
                        error_code=error_code,
                        error_message=error_message,
                        now=now,
                        expected_publication=expected_publication,
                    )

            class RacingUow(SqlAlchemyRunLifecycleUnitOfWork):
                @property
                def runs(self) -> RacingStore:
                    return RacingStore(self.session)

            def external(request: httpx.Request) -> httpx.Response:
                raise AssertionError("initial cancellation must start no HTTP")

            await PublishRunReview(
                uow_factory=lambda: RacingUow(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=Trace(),
                gitlab_publications=installed(fixture, external),
            ).execute(pointer)
            async with fixture.sessions() as session:
                actual = (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                ).one()
                assert tuple(actual) == (final_state, reason)
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM findings WHERE run_id=:id AND published"),
                        {"id": pointer.run_id},
                    )
                    == 0
                )

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("fault", ["operational", "pool_timeout"])
def test_final_checkpoint_only_sql_transient_uses_safe_existing_retry_and_markers(
    publication_database: tuple[str, str, UUID],
    fault: str,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

    from app.modules.reviews.application.check_runs import RunProviderCheckpoint
    from app.modules.reviews.application.publish_run_review import (
        GitLabPublicationGateway,
        PublishContext,
        ReviewSubmission,
        SubmittedReview,
    )

    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            armed = False
            failures = 0
            posts: list[str] = []
            pauses: list[float] = []
            calls: list[str] = []

            class CheckpointStore(SqlAlchemyRunLifecycleStore):
                async def publish_context(self, run_id: UUID) -> PublishContext | None:
                    nonlocal armed, failures
                    if armed:
                        armed = False
                        failures += 1
                        if fault == "operational":
                            raise OperationalError(
                                "private-query-canary", None, Exception("private-driver-canary")
                            )
                        raise SqlAlchemyTimeoutError("private-pool-canary")
                    return await super().publish_context(run_id)

            class CheckpointUow(SqlAlchemyRunLifecycleUnitOfWork):
                @property
                def runs(self) -> CheckpointStore:
                    return CheckpointStore(self.session)

            async def external(request: httpx.Request) -> httpx.Response:
                calls.append(request.method)
                if request.url.path.endswith("/versions"):
                    return wire(200, versions())
                if request.method == "GET":
                    return wire(200, [{"notes": [{"id": 701, "body": posts[0]}]}] if posts else [])
                posts.append(str(json.loads(request.content)["body"]))
                return wire(201, {"notes": [{"id": 701}]})

            real = installed(fixture, external)

            class FinalCheckpointResources:
                @asynccontextmanager
                async def open(
                    self, context: PublishContext, *, checkpoint: RunProviderCheckpoint
                ) -> AsyncIterator[GitLabPublicationGateway]:
                    async with real.open(context, checkpoint=checkpoint) as gateway:

                        class FinalGateway:
                            async def submit_review(
                                self, submission: ReviewSubmission, *, project_id: int
                            ) -> SubmittedReview:
                                nonlocal armed
                                result = await gateway.submit_review(
                                    submission, project_id=project_id
                                )
                                if failures == 0:
                                    armed = True  # Only after all HTTP response fences completed.
                                return result

                            async def checkpoint(self) -> None:
                                await gateway.checkpoint()

                        yield FinalGateway()

            async def pause(seconds: float) -> None:
                pauses.append(seconds)

            trace = Trace()
            await PublishRunReview(
                uow_factory=lambda: CheckpointUow(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=trace,
                gitlab_publications=FinalCheckpointResources(),
                sleep=pause,
            ).execute(pointer)
            assert failures == 1 and pauses == [2.0]
            assert calls == ["GET", "GET", "GET", "POST", "GET", "GET"] and len(posts) == 1
            assert trace.records[0][2] == {
                "error": {
                    "http_status": None,
                    "message": "GitLab publication authority unavailable",
                }
            }
            assert all(
                canary not in repr(trace.records)
                for canary in (
                    "private-query-canary",
                    "private-driver-canary",
                    "private-pool-canary",
                    "diff_fetch_failed",
                )
            )
            async with fixture.sessions() as session:
                assert (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                ).one() == ("succeeded", None)

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("boundary", ["early", "complete"])
@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("body", None),
        ("route", None),
        ("unchanged", "installation_unavailable"),
        ("closed", "pr_closed"),
        ("head", "superseded"),
        ("user", "cancelled_by_user"),
    ],
)
def test_inactive_publication_fences_payload_and_route_before_terminal_mutation(
    publication_database: tuple[str, str, UUID],
    boundary: str,
    mutation: str,
    reason: str | None,
) -> None:
    from app.modules.reviews.application.publish_run_review import PublishContext

    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions() as session:
                expected = await SqlAlchemyRunLifecycleStore(session).publish_context(
                    pointer.run_id
                )
            assert expected is not None and expected.vcs_input is not None
            change = expected.vcs_input.code_change_id
            repository = expected.vcs_input.repository_id

            async def mutate() -> None:
                async with fixture.sessions.begin() as session:
                    if mutation == "body":
                        await session.execute(
                            text(
                                "UPDATE runs SET review_body='newer inactive payload' WHERE id=:id"
                            ),
                            {"id": pointer.run_id},
                        )
                    elif mutation == "route":
                        await session.execute(
                            text("UPDATE code_changes SET external_number=19 WHERE id=:id"),
                            {"id": change},
                        )
                    elif mutation in {"closed", "head"}:
                        # All lower-priority facts are also true, proving current priority.
                        await session.execute(
                            text("UPDATE runs SET cancel_requested=true WHERE id=:id"),
                            {"id": pointer.run_id},
                        )
                        await session.execute(
                            text(
                                "UPDATE code_changes SET state=CAST(:state AS code_change_state),"
                                "head_sha=:head WHERE id=:id"
                            ),
                            {
                                "id": change,
                                "state": "closed" if mutation == "closed" else "open",
                                "head": "c" * 40,
                            },
                        )
                    elif mutation == "user":
                        await session.execute(
                            text("UPDATE runs SET cancel_requested=true WHERE id=:id"),
                            {"id": pointer.run_id},
                        )

            # Fixture-only availability change leaves a publishing row so the public
            # SQL boundary can observe an inactive origin; lifecycle revoke normally
            # terminalizes immediately. No supported activation/provider UPDATE claim.
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET installation_available=false WHERE id=:id"),
                    {"id": repository},
                )
            if boundary == "early":
                async with fixture.sessions() as session:
                    inactive = await SqlAlchemyRunLifecycleStore(session).publish_context(
                        pointer.run_id
                    )
                assert inactive is not None and not inactive.origin_active
                assert inactive.workspace_id is None and inactive.vcs_input is None
                assert inactive.publication_identity == expected.publication_identity
                assert inactive.publication_identity is not None

                class RacingStore(SqlAlchemyRunLifecycleStore):
                    async def finish(
                        self,
                        run_id: UUID,
                        *,
                        from_state: str,
                        worker_id: str | None,
                        state: str,
                        error_code: str | None,
                        error_message: str | None,
                        now: datetime,
                        expected_publication: PublishContext | None = None,
                    ) -> bool:
                        await mutate()
                        return await super().finish(
                            run_id,
                            from_state=from_state,
                            worker_id=worker_id,
                            state=state,
                            error_code=error_code,
                            error_message=error_message,
                            now=now,
                            expected_publication=expected_publication,
                        )

                class RacingUow(SqlAlchemyRunLifecycleUnitOfWork):
                    @property
                    def runs(self) -> RacingStore:
                        return RacingStore(self.session)

                def external(request: httpx.Request) -> httpx.Response:
                    raise AssertionError("inactive route must start no HTTP")

                await PublishRunReview(
                    uow_factory=lambda: RacingUow(fixture.sessions),
                    reviews=None,
                    check_runs=None,
                    trace=Trace(),
                    gitlab_publications=installed(fixture, external),
                ).execute(pointer)
            else:
                await mutate()  # The gap after the last active detached checkpoint.
                async with fixture.sessions.begin() as session:
                    result = await SqlAlchemyRunLifecycleStore(session).complete_publication(
                        pointer.run_id,
                        review_id=701,
                        comment_ids=(),
                        findings_hash=expected.findings_hash,
                        moved_to_body_finding_ids=tuple(
                            item.finding_id
                            for item in expected.findings
                            if item.finding_id is not None
                        ),
                        now=datetime.now(UTC),
                        expected_publication=expected,
                    )
                    assert result == ("cancelled" if reason else None)
            async with fixture.sessions() as session:
                state = (
                    await session.execute(
                        text("SELECT state::text,error_code,finished_at FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                ).one()
                assert (state[0], state[1]) == ("cancelled" if reason else "publishing", reason)
                assert (state[2] is None) is (reason is None)
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM findings WHERE run_id=:id AND published"),
                        {"id": pointer.run_id},
                    )
                    == 0
                )

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("boundary", ["early", "complete"])
def test_inactive_rebound_publication_notifies_only_locked_original_workspace(
    publication_database: tuple[str, str, UUID],
    boundary: str,
) -> None:
    import psycopg

    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions() as session:
                expected = await SqlAlchemyRunLifecycleStore(session).publish_context(
                    pointer.run_id
                )
            assert expected is not None and expected.vcs_input is not None
            original_repository = expected.vcs_input.repository_id
            replacement_workspace = uuid4()
            replacement_installation = uuid4()
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id,name,daily_budget_usd) "
                        "VALUES (:id,'replacement tenant',7)"
                    ),
                    {"id": replacement_workspace},
                )
            await fixture.management.create(
                installation_id=replacement_installation,
                workspace_id=replacement_workspace,
                instance_origin="https://other.gitlab.example",
                token="replacement-authority-token",
                webhook_secret="z" * 32,
            )
            # Synthetic race fixture ONLY: normal lifecycle replacement retires first.
            # Release the old reservation/rebind current ownership while old Run stays
            # publishing, so terminal notification partitioning can be observed directly.
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("DELETE FROM gitlab_project_bindings WHERE repository_id=:id"),
                    {"id": original_repository},
                )
                await session.execute(
                    text(
                        "UPDATE repositories SET provider_installation_id=:replacement WHERE id=:id"
                    ),
                    {"replacement": replacement_installation, "id": original_repository},
                )
            async with fixture.sessions() as session:
                inactive = await SqlAlchemyRunLifecycleStore(session).publish_context(
                    pointer.run_id
                )
            assert inactive is not None and not inactive.origin_active
            assert inactive.workspace_id is None and inactive.vcs_input is None
            assert inactive.installation_id is None
            assert inactive.publication_identity == expected.publication_identity
            assert inactive.publication_identity is not None
            assert inactive.publication_identity.origin_workspace_id == fixture.workspace
            dsn = publication_database[0].replace("postgresql+psycopg://", "postgresql://")
            with psycopg.connect(
                dsn, autocommit=True, application_name="dmc169-t14-notify-listener"
            ) as listener:
                listener.execute("LISTEN run_updated")
                if boundary == "early":

                    def external(request: httpx.Request) -> httpx.Response:
                        raise AssertionError("inactive rebound route must start no provider HTTP")

                    await PublishRunReview(
                        uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                        reviews=None,
                        check_runs=None,
                        trace=Trace(),
                        gitlab_publications=installed(fixture, external),
                    ).execute(pointer)
                else:
                    async with fixture.sessions.begin() as session:
                        assert (
                            await SqlAlchemyRunLifecycleStore(session).complete_publication(
                                pointer.run_id,
                                review_id=701,
                                comment_ids=(),
                                findings_hash=expected.findings_hash,
                                moved_to_body_finding_ids=(),
                                now=datetime.now(UTC),
                                expected_publication=expected,
                            )
                            == "cancelled"
                        )
                notices = [json.loads(notice.payload) for notice in listener.notifies(timeout=0.1)]
            assert notices == [
                {
                    "run_id": str(pointer.run_id),
                    "workspace_id": str(fixture.workspace),
                    "status": "cancelled",
                }
            ]
            assert all(notice["workspace_id"] != str(replacement_workspace) for notice in notices)

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("changed", ["findings", "head"])
def test_stale_installed_pointer_before_initial_inactive_snapshot_is_noop(
    publication_database: tuple[str, str, UUID],
    changed: str,
) -> None:
    import psycopg

    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions() as session:
                initial = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
            assert initial is not None and initial.vcs_input is not None
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET installation_available=false WHERE id=:id"),
                    {"id": initial.vcs_input.repository_id},
                )
                if changed == "findings":
                    await session.execute(
                        text(
                            "INSERT INTO findings (id,run_id,file_path,line_start,"
                            "severity,confidence,"
                            "category,title,body,published,inline_comment) VALUES "
                            "(:finding,:id,'x.py',1,'high',0.9,'correctness','New finding',"
                            "'New payload',false,true)"
                        ),
                        {"finding": uuid4(), "id": pointer.run_id},
                    )
                else:
                    await session.execute(
                        text("UPDATE runs SET head_sha=:head WHERE id=:id"),
                        {"id": pointer.run_id, "head": "c" * 40},
                    )
                    await session.execute(
                        text("UPDATE code_changes SET head_sha=:head WHERE id=:id"),
                        {"id": initial.vcs_input.code_change_id, "head": "c" * 40},
                    )
            async with fixture.sessions() as session:
                before_run = await session.scalar(
                    text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": pointer.run_id}
                )
                before_findings = (
                    await session.scalars(
                        text("SELECT to_jsonb(f) FROM findings f WHERE run_id=:id ORDER BY id"),
                        {"id": pointer.run_id},
                    )
                ).all()

            def external(request: httpx.Request) -> httpx.Response:
                raise AssertionError("stale inactive pointer must not start HTTP")

            trace = Trace()
            with psycopg.connect(
                publication_database[0].replace("postgresql+psycopg://", "postgresql://"),
                autocommit=True,
                application_name="dmc169-t14-stale-listener",
            ) as listener:
                listener.execute("LISTEN run_updated")
                await PublishRunReview(
                    uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                    reviews=None,
                    check_runs=None,
                    trace=trace,
                    gitlab_publications=installed(fixture, external),
                ).execute(pointer)
                notices = list(listener.notifies(timeout=0.1))
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT to_jsonb(r) FROM runs r WHERE id=:id"), {"id": pointer.run_id}
                    )
                    == before_run
                )
                assert (
                    await session.scalars(
                        text("SELECT to_jsonb(f) FROM findings f WHERE run_id=:id ORDER BY id"),
                        {"id": pointer.run_id},
                    )
                ).all() == before_findings
            assert notices == [] and trace.records == []

    asyncio.run(asyncio.wait_for(scenario(), 25))


@pytest.mark.parametrize("method", ["finish", "complete"])
@pytest.mark.parametrize("facts", ["closed", "head", "user"])
def test_legacy_inactive_default_keeps_installation_unavailable(
    publication_database: tuple[str, str, UUID],
    method: str,
    facts: str,
) -> None:
    async def scenario() -> None:
        async with rig(publication_database) as fixture:
            pointer = await publishing(fixture)
            async with fixture.sessions() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
            assert context is not None and context.vcs_input is not None
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET installation_available=false WHERE id=:id"),
                    {"id": context.vcs_input.repository_id},
                )
                await session.execute(
                    text("UPDATE runs SET cancel_requested=true WHERE id=:id"),
                    {"id": pointer.run_id},
                )
                if facts in {"closed", "head"}:
                    await session.execute(
                        text(
                            "UPDATE code_changes SET state=CAST(:state AS code_change_state),"
                            "head_sha=:head WHERE id=:id"
                        ),
                        {
                            "id": context.vcs_input.code_change_id,
                            "state": "closed" if facts == "closed" else "open",
                            "head": "c" * 40,
                        },
                    )
            # Deliberately OMIT expected_publication, preserving the original caller API.
            async with fixture.sessions.begin() as session:
                store = SqlAlchemyRunLifecycleStore(session)
                if method == "finish":
                    assert await store.finish(
                        pointer.run_id,
                        from_state="publishing",
                        worker_id=None,
                        state="failed",
                        error_code="legacy_failure",
                        error_message="safe",
                        now=datetime.now(UTC),
                    )
                else:
                    assert (
                        await store.complete_publication(
                            pointer.run_id,
                            review_id=701,
                            comment_ids=(),
                            findings_hash=pointer.findings_hash,
                            moved_to_body_finding_ids=(),
                            now=datetime.now(UTC),
                        )
                        == "cancelled"
                    )
            async with fixture.sessions() as session:
                assert (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                ).one() == ("cancelled", "installation_unavailable")

    asyncio.run(asyncio.wait_for(scenario(), 25))
