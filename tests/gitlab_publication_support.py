"""Shared installed GitLab publication fixtures for PostgreSQL tests."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text

from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.gitlab_publication_resources import (
    GitLabInstalledPublicationResources,
)
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from tests.gitlab_http_fakes import Stream


class Queue:
    def __init__(self) -> None:
        self.pointers: list[ReviewPublishPointer] = []

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        self.pointers.append(pointer)


async def seed_change(fixture: Any, repository: UUID) -> UUID:
    change = uuid4()
    async with fixture.sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO code_changes (id,repository_id,external_id,external_number,title,"
                "source_branch,target_branch,base_sha,head_sha,state,web_url) VALUES "
                "(:id,:repo,901,12,'Rerun MR','feature','main',:base,:head,'open',"
                "'https://gitlab.com/team/project/-/merge_requests/12')"
            ),
            {"id": change, "repo": repository, "base": "b" * 40, "head": "d" * 40},
        )
    return change


async def publishing(
    fixture: Any,
    *,
    seed_run: Callable[..., Awaitable[UUID]],
    origin: str = "https://gitlab.com",
    workspace: UUID | None = None,
    replacement: bool = False,
    historical: bool = True,
    answer: dict[str, object] | None = None,
    diff_patch: str | None = None,
) -> ReviewPublishPointer:
    """Seed installed publication; historical rows exercise the pre-plan fallback."""
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
        if diff_patch is not None:
            await session.execute(
                text(
                    "UPDATE repositories SET max_comments=1,severity_threshold='warning' "
                    "WHERE id=:id"
                ),
                {"id": target.repository_id},
            )
    run = await seed_run(fixture, target, change, state="running", attempt=1)
    async with fixture.sessions.begin() as session:
        await session.execute(
            text("UPDATE runs SET worker_id='publication' WHERE id=:id"), {"id": run}
        )
        if diff_patch is not None:
            await session.execute(
                text(
                    "INSERT INTO code_change_diffs (id,run_id,code_change_id,head_sha,"
                    "filename,status,patch,additions,deletions) VALUES "
                    "(:id,:run,:change,:head,'a.py','modified',:patch,2,1)"
                ),
                {
                    "id": uuid4(),
                    "run": run,
                    "change": change,
                    "head": "a" * 40,
                    "patch": diff_patch,
                },
            )
    queue = Queue()
    assert await StoreReviewOutput(
        lambda: SqlAlchemyReviewOutputUnitOfWork(fixture.sessions),
        queue,
        worker_id="publication",
        expected_attempt=1,
        run_url=lambda _run_id: None,
    ).execute(
        run,
        answer
        or {
            "findings": [],
            "summary": {
                "problem": "No issue found.",
                "done_well": "The code is clear.",
                "effort": "none",
            },
        },
    )
    if historical:
        async with fixture.sessions.begin() as session:
            await session.execute(
                text("DELETE FROM final_review_results WHERE run_id=:id"), {"id": run}
            )
            await session.execute(
                text("DELETE FROM run_actions WHERE run_id=:id AND tool='review.publish_start'"),
                {"id": run},
            )
            await session.execute(
                text(
                    "UPDATE run_actions SET tool='review.postprocess' "
                    "WHERE run_id=:id AND tool='review.finalize'"
                ),
                {"id": run},
            )
    return queue.pointers[0]


def installed(fixture: Any, external: Any) -> GitLabInstalledPublicationResources:
    return GitLabInstalledPublicationResources(
        session_factory=fixture.sessions,
        authority=fixture.authority,
        origin_policy=GitLabOriginPolicy(
            frozenset({"https://gitlab.com", "https://other.gitlab.example"})
        ),
        transport_factory=lambda: httpx.MockTransport(external),
    )


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
