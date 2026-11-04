"""A failed conventions tree read keeps its Retry-After up to the Run (#336, PIPELINE_SPEC §4.2).

Each case runs the real source over a synthetic HTTP answer, the real use case on a cache
miss and the real retry handler, so a hop that drops the hint fails here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import unquote
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.gitlab.infrastructure.run_http import (
    GitLabMergeRequestResource,
    GitLabRunHttp,
)
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    GenerateRepoConventions,
    PolicyFilteredConventionsSource,
    RepositoryConventionsSource,
)
from app.modules.reviews.application.handle_review_run import DeliveryOutcome
from app.modules.reviews.application.run_failures import RetryDelays, RunFailure
from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree
from app.modules.reviews.infrastructure.gitlab_run_source import GitLabRunSource
from app.modules.reviews.infrastructure.gitlab_vcs import HttpGitLabVcsProvider
from tests.test_gitlab_repository_sources import RUN_ID, SourceRouter, checkpoint
from tests.test_gitlab_run_http import INPUT, POLICY, Credentials
from tests.test_gitlab_vcs import reply
from tests.test_repo_conventions import FakeModel, FakeStore, FakeUnitOfWorkFactory, draft
from tests.test_review_run_worker import NOW, RUN, Clock, FakeRun, RetryQueue, handler

RETRY_AFTER_900 = {"Retry-After": "900"}

type GitLabFault = Callable[[httpx.Request], httpx.Response | None]


async def _cache_miss(source: RepositoryConventionsSource) -> None:
    """One conventions generation with nothing cached, behind the policy filter of a Run."""
    await GenerateRepoConventions(
        PolicyFilteredConventionsSource(source, lambda path: False),
        FakeModel(draft()),
        FakeUnitOfWorkFactory(FakeStore()),
    ).execute(
        repository_id=INPUT.repository_id,
        conventions_prompt=ActiveConventionsPrompt(UUID(int=19), "system"),
        run_id=RUN_ID,
        changed_files=("app/main.py",),
    )


def _assert_parks_for_15_minutes(failure: RunFailure) -> None:
    assert failure.retry_after == timedelta(seconds=900)
    assert RetryDelays().for_failure(1, failure) == ("10m", timedelta(minutes=15))
    run = FakeRun()
    retry = RetryQueue()

    def pipeline(claimed: object) -> bool:
        raise failure

    outcome = asyncio.run(handler(run, pipeline, retry=retry, clock=Clock()).execute(RUN))

    assert outcome is DeliveryOutcome.ACK
    assert (run.state, run.attempt) == ("queued", 1)
    assert retry.published == [("10m", 2)]
    assert run.available_at == NOW + timedelta(minutes=15)


def _assert_repeats_as_without_a_hint(failure: RunFailure) -> None:
    assert failure.retry_after is None
    delays = RetryDelays()
    assert delays.for_failure(1, failure) == ("30s", timedelta(seconds=30))
    assert delays.for_failure(2, failure) == ("2m", timedelta(minutes=2))


def _gitlab_fault(read: str, status: int, headers: dict[str, str]) -> GitLabFault:
    def fault(request: httpx.Request) -> httpx.Response | None:
        listing = (
            request.url.path.endswith("/repository/tree")
            and request.url.params.get("recursive") == "true"
        )
        size = request.method == "HEAD" and "notes.unknown" in request.url.path
        if (read == "listing" and listing) or (read == "sizes" and size):
            return reply(status=status, headers=headers)
        return None

    return fault


def _gitlab_failure(
    fault: GitLabFault | None = None, *, heads: list[str] | None = None, spend_budget: bool = False
) -> RunFailure:
    router = SourceRouter()

    def external(request: httpx.Request) -> httpx.Response:
        if heads is not None and request.method == "HEAD":
            heads.append(unquote(request.url.path.split("/repository/files/")[1]))
        answer = None if fault is None else fault(request)
        return router.response(request) if answer is None else answer

    async def scenario() -> RunFailure:
        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            source = GitLabRunSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                vcs=HttpGitLabVcsProvider(http=http),
                run_id=RUN_ID,
            )
            if spend_budget:
                # AGENTS.md is read and cached first, so the budget ends at the tree itself.
                await source.fetch_agents_md(INPUT.repository_id)
                while len(router.requests) < 512:
                    await http.get_json(GitLabMergeRequestResource(), max_bytes=1048576)
            with pytest.raises(RunFailure) as error:
                await _cache_miss(source)
            return error.value

    return asyncio.run(scenario())


@pytest.mark.parametrize("read", ["listing", "sizes"])
def test_gitlab_tree_retry_after_parks_the_run(read: str) -> None:
    failure = _gitlab_failure(_gitlab_fault(read, 429, RETRY_AFTER_900))

    assert failure.error_code == "diff_fetch_failed"
    _assert_parks_for_15_minutes(failure)


def test_gitlab_size_reads_stop_at_the_first_retry_after() -> None:
    heads: list[str] = []

    _gitlab_failure(_gitlab_fault("sizes", 429, RETRY_AFTER_900), heads=heads)

    # Sorted paths: nothing is asked for after the provider said to wait.
    assert heads == ["empty.py", "notes.unknown"]


@pytest.mark.parametrize("read", ["listing", "sizes"])
def test_gitlab_tree_failure_without_retry_after_repeats_as_before(read: str) -> None:
    failure = _gitlab_failure(_gitlab_fault(read, 500, {}))

    assert failure.error_code == "diff_fetch_failed"
    _assert_repeats_as_without_a_hint(failure)


def test_gitlab_size_read_without_retry_after_still_reads_the_other_files() -> None:
    heads: list[str] = []

    _gitlab_failure(_gitlab_fault("sizes", 500, {}), heads=heads)

    assert heads == ["empty.py", "notes.unknown", "src/app.py", "web/app.ts"]


def test_gitlab_budget_exhausted_tree_stays_a_failure_without_a_hint() -> None:
    failure = _gitlab_failure(spend_budget=True)

    assert failure.error_code == "diff_fetch_failed"
    _assert_repeats_as_without_a_hint(failure)
