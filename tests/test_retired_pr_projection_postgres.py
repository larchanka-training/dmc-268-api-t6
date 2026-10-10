"""PR retirement boundaries retain current history and isolate source receipts."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.webhooks.infrastructure.github_current_pull_request import (
    HttpGitHubCurrentPullRequestProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_reviewer_timeline import (
    HttpGitHubReviewerTimelineProvider,
)
from app.modules.reviews.application.project_github_pull_request import (
    ProjectGitHubPullRequest,
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestProjectionStatus,
    PullRequestProviderCheckpoint,
    PullRequestState,
    ReviewerTimelineSnapshot,
    TimelineLifecycle,
)
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionLock,
    SqlAlchemyPullRequestProjectionStore,
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from tests.portal_postgres import (
    HEAD,
    INSTALL_A,
    PR_NEW,
    REPO_A,
    RUN_ATTEMPTED,
    Env,
    portal_schema,
)
from tests.test_app_reinstall_postgres import FreshGitHub, apply, seed_archive
from tests.test_retired_installation_postgres import DiscardTokenCache

_NOW = datetime.now(UTC)


@pytest.fixture
def projection_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


def event(action: str) -> PullRequestEvent:
    return PullRequestEvent(
        action=action,
        installation_external_id=17,
        repository_external_id=101,
        external_id=903,
        number=3,
        title="Changed by source",
        description=None,
        author_login="octocat",
        web_url="https://github.test/octo/a/pull/3",
        source_branch="feature",
        target_branch="main",
        base_sha="b" * 40,
        head_sha="f" * 40,
        state=PullRequestState.CLOSED if action == "closed" else PullRequestState.OPEN,
        provider_updated_at=_NOW,
        current_label_names=frozenset({"ai-review"}),
    )


@dataclass
class Provider:
    factory: async_sessionmaker[AsyncSession]
    retire_after_timeline: bool = False
    rebind_during_current: bool = False
    calls: list[str] = field(default_factory=list)

    async def enter_provider(self, name: str) -> None:
        async with self.factory.begin() as session:
            await session.execute(
                text(
                    "SELECT id FROM provider_installations ORDER BY external_id FOR UPDATE NOWAIT"
                ),
            )
        self.calls.append(name)

    async def snapshot(
        self, event: PullRequestEvent, bot_login: str, *, checkpoint: PullRequestProviderCheckpoint
    ) -> ReviewerTimelineSnapshot:
        await self.enter_provider("timeline")
        if self.retire_after_timeline:
            async with self.factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                        "WHERE id=:id"
                    ),
                    {"id": INSTALL_A},
                )
        return ReviewerTimelineSnapshot(None, TimelineLifecycle(11, _NOW, "closed", 0))

    async def get_current(
        self, event: PullRequestEvent, *, checkpoint: PullRequestProviderCheckpoint
    ) -> PullRequestEvent:
        await self.enter_provider("current")
        if self.rebind_during_current:
            await self.rebind()
        return event

    async def rebind(self) -> None:
        await seed_archive(self.factory)
        await apply(self.factory, FreshGitHub())
        async with self.factory.begin() as session:
            await session.execute(
                text("UPDATE repositories SET enabled=true WHERE id=:id"), {"id": REPO_A}
            )
            await session.execute(
                text("UPDATE runs SET state='queued' WHERE id=:id"), {"id": RUN_ATTEMPTED}
            )


@pytest.mark.integration
@pytest.mark.parametrize("action", ["synchronize", "closed", "labeled"])
@pytest.mark.parametrize("retired", [False, True])
def test_projection_provider_reads_refuse_retired_source_and_keep_active_baseline(
    projection_env: Env,
    action: str,
    retired: bool,
) -> None:
    async def exercise() -> None:
        engine = projection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        provider = Provider(factory)
        try:
            if retired:
                async with factory.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                            "WHERE id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
            projector = ProjectGitHubPullRequest(
                uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(factory),
                bot_login="review-bot",
                current_provider=provider,
                reviewer_timeline_provider=provider,
                projection_lock=SqlAlchemyPullRequestProjectionLock(engine),
            )
            request = event(action)
            result = await projector.execute(
                PullRequestLabelEvent(request, "ai-review") if action == "labeled" else request
            )
            assert result is (
                PullRequestProjectionStatus.DISABLED_REPOSITORY
                if retired
                else PullRequestProjectionStatus.PROJECTED
            )
            assert provider.calls == (
                [] if retired else ["timeline", "current"] if action == "closed" else ["current"]
            )
            async with factory() as session:
                row = (
                    await session.execute(
                        text("SELECT title,head_sha FROM code_changes WHERE id=:id"), {"id": PR_NEW}
                    )
                ).one()
                assert tuple(row) == (
                    ("PR", HEAD)
                    if retired
                    else ("PR" if action == "closed" else "Changed by source", "f" * 40)
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_retiring_after_timeline_suppresses_next_current_provider_phase(
    projection_env: Env,
) -> None:
    async def exercise() -> None:
        engine = projection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        provider = Provider(factory, retire_after_timeline=True)
        try:
            projector = ProjectGitHubPullRequest(
                uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(factory),
                bot_login="review-bot",
                current_provider=provider,
                reviewer_timeline_provider=provider,
                projection_lock=SqlAlchemyPullRequestProjectionLock(engine),
            )
            assert (
                await projector.execute(event("closed"))
                is PullRequestProjectionStatus.DISABLED_REPOSITORY
            )
            assert provider.calls == ["timeline"]
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT state FROM runs WHERE id=:id"), {"id": RUN_ATTEMPTED}
                    )
                    == "queued"
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("action", ["synchronize", "labeled"])
@pytest.mark.parametrize("in_flight", [False, True])
def test_inflight_source_read_cannot_modify_or_cancel_rebound_target_history(
    projection_env: Env,
    action: str,
    in_flight: bool,
) -> None:
    async def exercise() -> None:
        engine = projection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        provider = Provider(factory, rebind_during_current=in_flight)
        try:
            if not in_flight:
                await provider.rebind()
            projector = ProjectGitHubPullRequest(
                uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(factory),
                bot_login="review-bot",
                current_provider=provider,
                projection_lock=SqlAlchemyPullRequestProjectionLock(engine),
            )
            request = event(action)
            result = await projector.execute(
                PullRequestLabelEvent(request, "ai-review") if action == "labeled" else request
            )
            assert result is PullRequestProjectionStatus.OTHER_INSTALLATION_REPOSITORY
            assert provider.calls == (["current"] if in_flight else [])
            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT title,head_sha,ai_review_labeled FROM code_changes WHERE id=:id"
                        ),
                        {"id": PR_NEW},
                    )
                ).one()
                assert tuple(row) == ("PR", HEAD, False)
                assert (
                    await session.scalar(
                        text("SELECT state FROM runs WHERE id=:id"), {"id": RUN_ATTEMPTED}
                    )
                    == "queued"
                )
            fresh = replace(request, installation_external_id=29)
            provider.rebind_during_current = False
            assert (
                await projector.execute(
                    PullRequestLabelEvent(fresh, "ai-review") if action == "labeled" else fresh
                )
                is PullRequestProjectionStatus.PROJECTED
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_projection_apply_holds_source_installation_lock_before_pr_write(
    projection_env: Env,
) -> None:
    async def exercise() -> None:
        engine = projection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                store = SqlAlchemyPullRequestProjectionStore(session)
                assert await store.get_or_create_locked(event("synchronize"), _NOW) is not None
                async with factory.begin() as concurrent:
                    with pytest.raises(DBAPIError) as blocked:
                        await concurrent.execute(
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


@pytest.mark.integration
@pytest.mark.parametrize(
    "repository_external_id,status",
    [
        (101, PullRequestProjectionStatus.OTHER_INSTALLATION_REPOSITORY),
        (113404, PullRequestProjectionStatus.UNKNOWN_REPOSITORY),
    ],
)
def test_unknown_source_preserves_repository_refusal_without_provider_reads(
    projection_env: Env,
    repository_external_id: int,
    status: PullRequestProjectionStatus,
) -> None:
    async def exercise() -> None:
        engine = projection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        provider = Provider(factory)
        try:
            projector = ProjectGitHubPullRequest(
                uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(factory),
                bot_login="review-bot",
                current_provider=provider,
                projection_lock=SqlAlchemyPullRequestProjectionLock(engine),
            )
            source = replace(
                event("labeled"),
                installation_external_id=113404,
                repository_external_id=repository_external_id,
            )
            assert await projector.execute(PullRequestLabelEvent(source, "ai-review")) is status
            assert provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["current", "label", "timeline_token", "timeline_page"])
@pytest.mark.parametrize("retire", [False, True])
def test_projection_stops_next_http_after_token_or_timeline_page_retirement(
    projection_env: Env, phase: str, retire: bool
) -> None:
    async def exercise() -> None:
        engine = projection_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls: list[str] = []
        action = (
            "closed"
            if phase.startswith("timeline")
            else "labeled"
            if phase == "label"
            else "synchronize"
        )
        request_event = replace(event(action), repository_full_name="octo/a")

        async def retire_source() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE provider_installations SET deleted_at=clock_timestamp() "
                        "WHERE id=:id"
                    ),
                    {"id": INSTALL_A},
                )

        async def http(request: httpx.Request) -> httpx.Response:
            async with factory.begin() as session:
                await session.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": INSTALL_A},
                )
            path = request.url.path
            if path.endswith("/access_tokens"):
                calls.append("token")
                assert path == "/app/installations/17/access_tokens"
                if retire and phase != "timeline_page":
                    await retire_source()
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if path.endswith("/timeline"):
                page = int(request.url.params["page"])
                calls.append(f"timeline{page}")
                if retire and phase == "timeline_page" and page == 1:
                    await retire_source()
                indices = range(1, 101) if phase == "timeline_page" else range(1, 2)
                items = (
                    [
                        {"id": index, "event": "closed", "created_at": _NOW.isoformat()}
                        for index in indices
                    ]
                    if page == 1
                    else []
                )
                return httpx.Response(200, json=items)
            calls.append("current")
            assert path == "/repos/octo/a/pulls/3"
            return httpx.Response(
                200,
                json={
                    "id": 903,
                    "number": 3,
                    "title": "Fresh provider title",
                    "body": None,
                    "html_url": "https://github.test/octo/a/pull/3",
                    "user": {"login": "octocat"},
                    "head": {"ref": "feature", "sha": "f" * 40},
                    "base": {"ref": "main", "sha": "b" * 40},
                    "state": "closed" if phase.startswith("timeline") else "open",
                    "merged": False,
                    "updated_at": _NOW.isoformat(),
                    "labels": [{"name": "ai-review"}],
                },
            )

        try:
            async with factory() as session:
                before_pr = await session.scalar(
                    text("SELECT row_to_json(c) FROM code_changes c WHERE id=:id"), {"id": PR_NEW}
                )
                before_runs = (
                    (await session.execute(text("SELECT row_to_json(r) FROM runs r ORDER BY id")))
                    .scalars()
                    .all()
                )
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=time.time,
                )
                projector = ProjectGitHubPullRequest(
                    uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(factory),
                    bot_login="review-bot",
                    projection_lock=SqlAlchemyPullRequestProjectionLock(engine),
                    current_provider=HttpGitHubCurrentPullRequestProvider(
                        client=client, token_provider=tokens
                    ),
                    reviewer_timeline_provider=HttpGitHubReviewerTimelineProvider(
                        client=client, token_provider=tokens
                    ),
                )
                result = await projector.execute(
                    PullRequestLabelEvent(request_event, "ai-review")
                    if phase == "label"
                    else request_event
                )
            assert result is (
                PullRequestProjectionStatus.DISABLED_REPOSITORY
                if retire
                else PullRequestProjectionStatus.PROJECTED
            )
            if retire:
                assert calls == (["token", "timeline1"] if phase == "timeline_page" else ["token"])
            else:
                assert (
                    calls
                    == {
                        "current": ["token", "current"],
                        "label": ["token", "current"],
                        "timeline_token": ["token", "timeline1", "token", "current"],
                        "timeline_page": ["token", "timeline1", "timeline2", "token", "current"],
                    }[phase]
                )
            async with factory() as session:
                after_pr = await session.scalar(
                    text("SELECT row_to_json(c) FROM code_changes c WHERE id=:id"), {"id": PR_NEW}
                )
                if retire:
                    assert after_pr == before_pr
                else:
                    assert (
                        await session.execute(
                            text("SELECT title,head_sha,state FROM code_changes WHERE id=:id"),
                            {"id": PR_NEW},
                        )
                    ).one() == (
                        "PR" if phase.startswith("timeline") else "Fresh provider title",
                        "f" * 40,
                        "closed" if phase.startswith("timeline") else "open",
                    )
                after_runs = (
                    (await session.execute(text("SELECT row_to_json(r) FROM runs r ORDER BY id")))
                    .scalars()
                    .all()
                )
                if retire:
                    assert after_runs == before_runs
                else:
                    # Normal current-source projection still cancels obsolete running work.
                    assert (
                        await session.execute(
                            text("SELECT state,error_code FROM runs WHERE id=:id"),
                            {"id": RUN_ATTEMPTED},
                        )
                    ).one() == (
                        "cancelled",
                        "pr_closed" if phase.startswith("timeline") else "superseded",
                    )
                    assert [row for row in after_runs if row["id"] != str(RUN_ATTEMPTED)] == [
                        row for row in before_runs if row["id"] != str(RUN_ATTEMPTED)
                    ]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
