"""Detached provider routing is immutable and comes from the persisted run origin."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunVcsInput
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import INSTALL_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_run_origin_guard_postgres import prepare
from tests.trigger_uow import processing_uow

ORIGIN = UUID("00000000-0000-4000-8000-000000000017")
REPOSITORY = UUID("00000000-0000-4000-8000-000000000101")
CHANGE = UUID("00000000-0000-4000-8000-000000000007")
RUN = UUID("00000000-0000-4000-8000-000000000001")
HEAD = "a" * 40
START = "b" * 40


def test_gitlab_routing_input_keeps_immutable_origin_and_numeric_project() -> None:
    locator = PullRequestLocator(
        17,
        "group/nested/project",
        7,
        provider="gitlab",
        origin_provider_installation_id=ORIGIN,
        repository_external_id=101,
    )
    run = RunVcsInput(CHANGE, REPOSITORY, HEAD, START, locator)

    assert run.provider == "gitlab"
    assert run.origin_provider_installation_id == ORIGIN
    assert run.repository_external_id == 101
    assert run.base_sha == "b" * 40
    with pytest.raises(FrozenInstanceError):
        locator.provider = "github"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("provider", "origin", "project", "message"),
    [
        ("unknown", ORIGIN, 101, "unsupported VCS provider"),
        ("gitlab", None, 101, "GitLab routing requires an immutable origin"),
        ("gitlab", ORIGIN, None, "GitLab routing requires a positive project ID"),
        ("gitlab", ORIGIN, 0, "GitLab routing requires a positive project ID"),
        ("gitlab", ORIGIN, -1, "GitLab routing requires a positive project ID"),
    ],
)
def test_invalid_routing_fails_at_the_application_boundary(
    provider: str, origin: UUID | None, project: int | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        PullRequestLocator(
            17,
            "group/nested/project",
            7,
            provider=provider,
            origin_provider_installation_id=origin,
            repository_external_id=project,
        )


def test_existing_github_locator_keeps_its_public_construction_contract() -> None:
    locator = PullRequestLocator(17, "octo/repo", 7)
    assert locator.provider == "github"
    assert locator.installation_external_id == 17
    assert locator.repository_full_name == "octo/repo"
    assert locator.number == 7


class RunInputs:
    def __init__(self, locator: PullRequestLocator) -> None:
        self.current: RunVcsInput | None = RunVcsInput(CHANGE, REPOSITORY, HEAD, START, locator)
        self.snapshots: list[DiffSnapshot] = []

    async def get_run_diff_input(self, run_id: UUID) -> None:
        raise AssertionError("detached VCS routing must not use legacy diff input")

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput | None:
        return self.current

    async def get_run_snapshots(self, run_id: UUID) -> None:
        return None

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        self.snapshots = snapshots
        return snapshots


class RecordingVcs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, PullRequestLocator]] = []

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        await checkpoint()
        self.calls.append(("metadata", locator))
        return VcsPullRequest(
            locator,
            HEAD,
            START,
            PullRequestMeta("MR", None, "dev", "feature", "main", (), 1, 1, 0, False, False),
        )

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        self.calls.append(("diff", pull_request.locator))
        return (VcsFile("a.py", "added", "d" * 40, None, 1, 0, 1, "@@ -0,0 +1 @@\n+ok"),)

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        self.calls.append(("blob", locator))
        return b"ok\n"


class ProviderSelection:
    def __init__(self, provider: RecordingVcs | None) -> None:
        self.provider = provider
        self.locators: list[PullRequestLocator] = []

    def select(self, locator: PullRequestLocator) -> RecordingVcs | None:
        self.locators.append(locator)
        return self.provider


def gitlab_locator() -> PullRequestLocator:
    return PullRequestLocator(17, "group/nested/project", 7, "gitlab", ORIGIN, 101)


def test_processor_selects_all_vcs_reads_from_detached_gitlab_identity() -> None:
    locator = gitlab_locator()
    runs = RunInputs(locator)
    gitlab = RecordingVcs()
    selection = ProviderSelection(gitlab)
    processor = ReviewRunProcessor(
        runs,
        None,
        vcs_provider_selector=selection,
        uow_factory=processing_uow(runs),
    )

    assert asyncio.run(processor.prepare(RUN)) is True
    assert selection.locators == [locator]
    assert gitlab.calls == [("metadata", locator), ("diff", locator), ("blob", locator)]
    assert runs.snapshots[0].blob_sha == "d" * 40


@pytest.mark.parametrize("configured_selector", [False, True])
def test_gitlab_without_an_explicit_provider_never_uses_github_fallback(
    configured_selector: bool,
) -> None:
    runs = RunInputs(gitlab_locator())
    github = RecordingVcs()
    selection = ProviderSelection(None)
    processor = ReviewRunProcessor(
        runs,
        None,
        vcs_provider=github,
        vcs_provider_selector=selection if configured_selector else None,
        uow_factory=processing_uow(runs),
    )

    with pytest.raises(ValueError, match="no VCS provider configured for gitlab"):
        asyncio.run(processor.prepare(RUN))
    assert github.calls == []


@pytest.mark.parametrize("change", ["missing", "origin", "project", "provider"])
def test_a_changed_origin_stops_the_next_provider_request(change: str) -> None:
    locator = gitlab_locator()
    runs = RunInputs(locator)

    class ChangingVcs(RecordingVcs):
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            result = await super().get_pull_request(locator, checkpoint=checkpoint)
            if change == "missing":
                runs.current = None
            else:
                changed = replace(
                    locator,
                    origin_provider_installation_id=UUID(int=99) if change == "origin" else ORIGIN,
                    repository_external_id=999 if change == "project" else 101,
                    provider="github" if change == "provider" else "gitlab",
                )
                runs.current = RunVcsInput(CHANGE, REPOSITORY, HEAD, START, changed)
            return result

    gitlab = ChangingVcs()
    processor = ReviewRunProcessor(
        runs,
        None,
        vcs_provider_selector=ProviderSelection(gitlab),
        uow_factory=processing_uow(runs),
    )

    with pytest.raises(RunCancelled, match="installation_unavailable"):
        asyncio.run(processor.prepare(RUN))
    assert gitlab.calls == [("metadata", locator)]
    assert runs.snapshots == []


@pytest.fixture
def routing_database() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to test persisted run routing")
    with portal_schema(database_url, None) as env:
        yield env


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_repository_reads_provider_origin_and_project_from_persisted_run(
    routing_database: Env, provider: str
) -> None:
    async def exercise() -> None:
        engine = routing_database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", "running")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET provider=:provider WHERE id=:id"),
                    {"provider": provider, "id": INSTALL_A},
                )
            result = await SqlAlchemyRunRepository(factory, allow_unscoped=True).get_run_vcs_input(
                RUN_ATTEMPTED
            )
            assert result is not None
            assert result.provider == provider
            assert result.origin_provider_installation_id == INSTALL_A
            assert result.repository_external_id == 101
            assert result.locator.installation_external_id == 17
            assert result.locator.repository_full_name == "octo/a"
            assert result.locator.number == 3
            assert result.base_sha == "b" * 40
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("origin", ["unknown", "rebound", "retired", "repo_removed"])
def test_repository_does_not_route_a_missing_or_mismatched_origin(
    routing_database: Env, origin: str
) -> None:
    async def exercise() -> None:
        engine = routing_database.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, origin, "running")
            assert (
                await SqlAlchemyRunRepository(factory, allow_unscoped=True).get_run_vcs_input(
                    RUN_ATTEMPTED
                )
                is None
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_provider_metadata_cannot_replace_the_requested_origin() -> None:
    locator = gitlab_locator()

    class WrongOrigin(RecordingVcs):
        async def get_pull_request(
            self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
        ) -> VcsPullRequest:
            result = await super().get_pull_request(locator, checkpoint=checkpoint)
            return replace(
                result, locator=replace(locator, origin_provider_installation_id=UUID(int=99))
            )

    async def checkpoint() -> None:
        return None

    gitlab = WrongOrigin()
    with pytest.raises(ValueError, match="head SHA changed"):
        asyncio.run(
            FetchVcsReviewInput(gitlab, checkpoint=checkpoint).execute(locator, HEAD, START)
        )
    assert gitlab.calls == [("metadata", locator)]


@pytest.mark.parametrize("field", ["origin_provider_installation_id", "repository_external_id"])
def test_credentials_reject_a_lost_authority_id_before_resolution(field: str) -> None:
    """A malformed adapter value must fail closed even if Python assertions are disabled."""
    from typing import Any, cast

    from sqlalchemy.ext.asyncio import AsyncSession

    import app.modules.integrations.gitlab.infrastructure.run_credentials as credentials_module
    from app.modules.integrations.gitlab.infrastructure.run_credentials import GitLabRunCredentials

    locator = gitlab_locator()
    runs = RunInputs(locator)
    calls: list[object] = []

    class Authority:
        async def resolve_target(self, identity: object) -> None:
            calls.append(identity)
            raise RuntimeError("invalid identity reached authority")

    async def checkpoint() -> None:
        pass

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(credentials_module, "SqlAlchemyRunRepository", lambda *a, **kw: runs)
        assert runs.current is not None
        credentials = GitLabRunCredentials(
            session_factory=cast(async_sessionmaker[AsyncSession], object()),
            authority=cast(Any, Authority()),
            run_id=RUN,
            expected_workspace_id=UUID(int=9),
            expected_input=runs.current,
            checkpoint=checkpoint,
        )
        # Model a malformed detached adapter value after initial construction; the boundary
        # must reject it before constructing the authority's target identity.
        object.__setattr__(locator, field, None)
        with pytest.raises(RunCancelled, match="installation_unavailable"):
            asyncio.run(credentials.bind())
        if calls:
            raise AssertionError("authority was called with a missing ID")
