"""Stored #171 coordinates and creation pins through installed GitLab ports (#169)."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.modules.reviews.application.publish_run_review import PublishRunReview
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.rerun_run import RerunRun
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunUnitOfWork
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests import test_gitlab_projection_postgres as projection
from tests.test_gitlab_projection_postgres import NOW, rig
from tests.test_gitlab_publication_postgres import installed, publishing, versions, wire
from tests.test_gitlab_run_trigger_postgres import HEAD, Publisher, enqueue, expected, seed_change
from tests.test_publish_run_review import Trace

rebase_database = projection.authority_database
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("move_head", [False, True])
def test_installed_publication_preserves_stored_coordinates_placement_and_head_fence(
    rebase_database: tuple[str, str, UUID], move_head: bool
) -> None:
    async def scenario() -> None:
        async with rig(rebase_database) as fixture:
            pointer = await publishing(fixture)
            context_id, range_id, pending_id = UUID(int=1), UUID(int=2), UUID(int=3)
            async with fixture.sessions.begin() as session:
                change = await session.scalar(
                    text("SELECT code_change_id FROM runs WHERE id=:id"), {"id": pointer.run_id}
                )
                await session.execute(
                    text(
                        "INSERT INTO code_change_diffs (id,run_id,code_change_id,head_sha,"
                        "filename,previous_filename,status,patch,additions,deletions) VALUES "
                        "(:id,:run,:change,:head,'a','old/a.py','renamed',:patch,2,1)"
                    ),
                    {
                        "id": uuid4(),
                        "run": pointer.run_id,
                        "change": change,
                        "head": HEAD,
                        "patch": (
                            "@@ -10,3 +10,4 @@\n before()\n-old_value()\n"
                            "+new_value()\n+validate()\n after()"
                        ),
                    },
                )
                for finding_id, start, end in (
                    (context_id, 13, None),
                    (range_id, 11, 12),
                    (pending_id, 12, None),
                ):
                    await session.execute(
                        text(
                            "INSERT INTO findings (id,run_id,file_path,line_start,line_end,"
                            "severity,confidence,category,title,body,published,inline_comment) "
                            "VALUES (:id,:run,'a',:start,:end,'high',0.9,'correctness',"
                            "'Finding','Body',false,true)"
                        ),
                        {"id": finding_id, "run": pointer.run_id, "start": start, "end": end},
                    )
            async with fixture.sessions() as session:
                context = await SqlAlchemyRunLifecycleStore(session).publish_context(pointer.run_id)
            assert context is not None
            pointer = ReviewPublishPointer(pointer.run_id, HEAD, context.findings_hash, "COMMENT")
            calls: list[str] = []
            posts: list[dict[str, object]] = []
            inline_posted = False

            async def external(request: httpx.Request) -> httpx.Response:
                nonlocal inline_posted
                assert request.url.host == "gitlab.com"
                assert request.headers["PRIVATE-TOKEN"] == "synthetic-authority-PAT-canary"
                # The network boundary leaves the only SQL lease and Run lock available.
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"),
                        {"id": pointer.run_id},
                    )
                calls.append(request.method + " " + request.url.path.rsplit("/", 1)[-1])
                if request.url.path.endswith("/versions"):
                    payload = versions()
                    if move_head and inline_posted:
                        payload[0]["head_commit_sha"] = "c" * 40
                    return wire(200, payload)
                if request.method == "GET":
                    return wire(200, [])
                payload = json.loads(request.content)
                posts.append(payload)
                if "position" not in payload:
                    return wire(201, {"notes": [{"id": 703}]})
                if inline_posted:
                    return wire(422, {"message": "position rejected"})
                inline_posted = True
                return wire(201, {"notes": [{"id": 701}]})

            await PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(fixture.sessions),
                reviews=None,
                check_runs=None,
                trace=Trace(),
                gitlab_publications=installed(fixture, external),
            ).execute(pointer)
            assert posts[0]["position"] == {
                "position_type": "text",
                "base_sha": "b" * 40,
                "start_sha": "b" * 40,
                "head_sha": HEAD,
                "old_path": "old/a.py",
                "new_path": "a",
                "old_line": 12,
                "new_line": 13,
            }
            assert calls[:4] == [
                "GET versions",
                "GET discussions",
                "GET versions",
                "POST discussions",
            ]
            async with fixture.sessions() as session:
                state = (
                    await session.execute(
                        text("SELECT state::text,error_code FROM runs WHERE id=:id"),
                        {"id": pointer.run_id},
                    )
                ).one()
                placement: dict[UUID, bool] = {
                    row[0]: row[1]
                    for row in (
                        (
                            await session.execute(
                                text("SELECT id,inline_comment FROM findings WHERE run_id=:id"),
                                {"id": pointer.run_id},
                            )
                        ).all()
                    )
                }
            if move_head:
                assert state == ("cancelled", "superseded")
                assert calls[-1] == "GET versions" and len(posts) == 1
                assert placement == {context_id: True, range_id: True, pending_id: True}
            else:
                assert state == ("succeeded", None)
                assert posts[1]["position"] == {
                    "position_type": "text",
                    "base_sha": "b" * 40,
                    "start_sha": "b" * 40,
                    "head_sha": HEAD,
                    "old_path": "old/a.py",
                    "new_path": "a",
                    "new_line": 12,
                    "line_range": {
                        "start": {
                            "line_code": "86f7e437faa5a7fce15d1ddcb9eaeaea377667b8_12_11",
                            "type": "new",
                            "new_line": 11,
                        },
                        "end": {
                            "line_code": "86f7e437faa5a7fce15d1ddcb9eaeaea377667b8_12_12",
                            "type": "new",
                            "new_line": 12,
                        },
                    },
                }
                assert "position" not in posts[2]
                assert calls[-2:] == ["GET versions", "POST discussions"]
                assert placement == {context_id: True, range_id: False, pending_id: False}

    asyncio.run(asyncio.wait_for(scenario(), 25))


def test_gitlab_enqueue_and_rerun_capture_current_publication_and_prompt_pins(
    rebase_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(rebase_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target)
            conventions, verifier = uuid4(), uuid4()
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE repositories SET severity_threshold='warning',"
                        "max_comments=3 WHERE id=:id"
                    ),
                    {"id": target.repository_id},
                )
                for key, row_id in (
                    ("review.conventions", conventions),
                    ("review.verifier", verifier),
                ):
                    await session.execute(
                        text(
                            "INSERT INTO prompt_versions "
                            "(id,key,version,content,checksum,is_active) "
                            "VALUES (:id,:key,1,:content,:checksum,true)"
                        ),
                        {
                            "id": row_id,
                            "key": key,
                            "content": f"synthetic {key} prompt",
                            "checksum": sha256(f"synthetic {key} prompt".encode()).hexdigest(),
                        },
                    )
            publisher = Publisher(fixture, target)
            result = await enqueue(fixture, publisher).execute(
                change, HEAD, expected_identity=expected(target)
            )
            assert result.run_id is not None
            async with fixture.sessions.begin() as session:
                pinned = (
                    await session.execute(
                        text(
                            "SELECT severity_threshold::text,max_comments,"
                            "conventions_prompt_version_id,"
                            "verifier_prompt_version_id FROM runs WHERE id=:id"
                        ),
                        {"id": result.run_id},
                    )
                ).one()
                assert pinned == ("warning", 3, conventions, verifier)
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE id=:id"), {"id": result.run_id}
                )
                await session.execute(
                    text(
                        "UPDATE repositories SET severity_threshold='critical',"
                        "max_comments=1 WHERE id=:id"
                    ),
                    {"id": target.repository_id},
                )
                await session.execute(
                    text("UPDATE prompt_versions SET is_active=false WHERE id=:id"),
                    {"id": verifier},
                )
            created = await RerunRun(
                lambda: SqlAlchemyRerunUnitOfWork(fixture.sessions, allow_unscoped=True),
                SqlAlchemyRunRepository(fixture.sessions, allow_unscoped=True),
                publisher,
                now=lambda: NOW,
            ).execute(result.run_id)
            assert created is not None
            async with fixture.sessions() as session:
                assert (
                    await session.execute(
                        text(
                            "SELECT severity_threshold::text,max_comments,"
                            "conventions_prompt_version_id,"
                            "verifier_prompt_version_id FROM runs WHERE id=:id"
                        ),
                        {"id": created.id},
                    )
                ).one() == ("critical", 1, conventions, None)
                assert (
                    await session.execute(
                        text(
                            "SELECT severity_threshold::text,max_comments,"
                            "conventions_prompt_version_id,"
                            "verifier_prompt_version_id FROM runs WHERE id=:id"
                        ),
                        {"id": result.run_id},
                    )
                ).one() == pinned

    asyncio.run(asyncio.wait_for(scenario(), 25))
