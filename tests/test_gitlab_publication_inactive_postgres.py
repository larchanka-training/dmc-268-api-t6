"""Inactive GitLab publication pointers and legacy terminal calls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.modules.reviews.application.publish_run_review import PublishRunReview
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
)
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import rig
from tests.test_gitlab_publication_postgres import installed, publishing
from tests.test_publish_run_review import Trace

publication_database = projection.authority_database
pytestmark = pytest.mark.integration


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
