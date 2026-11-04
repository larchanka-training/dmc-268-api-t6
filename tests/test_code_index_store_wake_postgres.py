"""PostgreSQL evidence for the wake-up marker (#143): triggers request a build, the leader
clears it with a CAS.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards; the fixtures and the
store harness live in `tests/code_index_postgres.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

from app.modules.code_index.application.index_store import (
    ClaimRefusal,
    ClaimRefused,
    IndexBuildStats,
    IndexProfile,
)
from app.modules.code_index.domain.enums import IndexStatus, PurgeReason
from tests.code_index_postgres import (
    _ADMISSION,
    _PROFILE,
    C1,
    C2,
    C3,
    _build,
    _claim,
    _enable,
    _Index,
    _mark_failed,
    _marker,
    _module,
    _publish,
    _purge,
    _release,
    _rows,
    _state,
    _try_claim,
    _write,
)

pytest_plugins = ("tests.code_index_postgres",)


async def _request_build(index: _Index, repository_id: UUID, target: str | None) -> bool:
    async with index.uow() as uow:
        requested = await uow.index.request_build(repository_id, target_commit_sha=target)
        await uow.commit()
    return requested


async def _reconcile(
    index: _Index,
    repository_id: UUID,
    *,
    generation: int = 1,
    branch: str = "main",
    commit: str = C1,
    read_at: datetime | None = None,
    versions_current: Callable[[IndexProfile], bool] = lambda profile: True,
) -> bool:
    """`reconcile_head` with a head read just now, unless `read_at` is given."""
    if read_at is None:
        [(read_at,)] = await _rows(index, "SELECT clock_timestamp()")
    async with index.uow() as uow:
        marked = await uow.index.reconcile_head(
            repository_id,
            generation=generation,
            branch=branch,
            commit_sha=commit,
            read_at=read_at,
            versions_current=versions_current,
        )
        await uow.commit()
    return marked


async def _clear_wake(index: _Index, repository_id: UUID, requested_at: datetime) -> bool:
    async with index.uow() as uow:
        cleared = await uow.index.clear_wake_request(repository_id, requested_at)
        await uow.commit()
    return cleared


async def _unmark(index: _Index, repository_id: UUID) -> None:
    marker = await _marker(index, repository_id)
    assert marker is not None
    assert await _clear_wake(index, repository_id, marker)


@pytest.mark.integration
def test_enable_sets_the_marker_on_creation_and_re_arm_but_not_on_a_live_or_failed_index(
    index: _Index,
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        created = await _marker(index, index.a)
        assert created is not None
        await _unmark(index, index.a)
        # A repeated enable of a live index is a no-op: no marker.
        await _enable(index, index.a)
        assert await _marker(index, index.a) is None

        for reason in (PurgeReason.DISABLED, PurgeReason.REMOVED, PurgeReason.TTL):
            await _purge(index, index.a, reason)
            assert await _marker(index, index.a) is None
            await _enable(index, index.a)
            rearmed = await _marker(index, index.a)
            assert rearmed is not None and rearmed > created
            await _unmark(index, index.a)

        # Three purges and three re-arms after creation: generation 7.
        fence = await _claim(index, index.a, 7)
        await _mark_failed(index, index.a, fence, "archive_unsafe")
        await _enable(index, index.a)
        state = await _state(index, index.a)
        assert state is not None and (state.generation, state.status) == (7, "failed")
        assert await _marker(index, index.a) is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_purge_mark_failed_and_a_quota_claim_clear_the_marker(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        assert await _marker(index, index.a) is not None
        await _purge(index, index.a)
        assert await _marker(index, index.a) is None

        await _enable(index, index.b)
        fence = await _claim(index, index.b)
        assert await _request_build(index, index.b, C2)
        await _mark_failed(index, index.b, fence, "limit_exceeded:edges")
        assert await _marker(index, index.b) is None

        # Purged at generation 2 and re-armed at 3, A publishes and then exceeds its quota.
        await _enable(index, index.a)
        fence = await _claim(index, index.a, 3)
        await _write(index, index.a, fence, _module("aa01", 1))
        assert await _publish(index, index.a, fence, C1)
        assert await _request_build(index, index.a, C2)
        over_quota = dataclasses.replace(_ADMISSION, repository_storage_bytes=1)
        # Without the clear the CHECK would refuse the `failed` row the claim writes.
        assert await _try_claim(index, index.a, 3, over_quota) == ClaimRefused(
            ClaimRefusal.QUOTA, "limit_exceeded:repository_storage"
        )
        state = await _state(index, index.a)
        assert state is not None and (state.status, state.target_commit_sha) == ("failed", C2)
        assert await _marker(index, index.a) is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_request_build_writes_the_target_and_always_refreshes_the_marker(index: _Index) -> None:
    async def scenario() -> None:
        # No state row: nothing to request.
        assert not await _request_build(index, index.a, C1)
        assert await _rows(index, "SELECT count(*) FROM code_index_states") == [(0,)]

        await _enable(index, index.a)
        first = await _marker(index, index.a)
        assert await _request_build(index, index.a, C2)
        second = await _marker(index, index.a)
        assert first is not None and second is not None and second > first
        state = await _state(index, index.a)
        assert state is not None and state.target_commit_sha == C2
        # Without a target only the marker moves; the target stays.
        assert await _request_build(index, index.a, None)
        third = await _marker(index, index.a)
        assert third is not None and third > second
        state = await _state(index, index.a)
        assert state is not None and state.target_commit_sha == C2

        await _purge(index, index.a)
        assert not await _request_build(index, index.a, C3)
        purged = await _state(index, index.a)
        assert purged is not None and purged.target_commit_sha is None
        assert await _marker(index, index.a) is None

        await _enable(index, index.b)
        fence = await _claim(index, index.b)
        await _mark_failed(index, index.b, fence, "archive_unsafe")
        assert not await _request_build(index, index.b, C3)
        failed = await _state(index, index.b)
        assert failed is not None and failed.target_commit_sha is None
        assert await _marker(index, index.b) is None

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("head", "marked"),
    [
        pytest.param({}, False, id="equal"),
        pytest.param({"commit": C2}, True, id="sha-changed"),
        pytest.param({"branch": "develop"}, True, id="branch-changed"),
        pytest.param({"versions_current": lambda profile: False}, True, id="versions-outdated"),
    ],
)
def test_reconcile_head_rewrites_the_target_and_marks_a_drifted_index(
    index: _Index, head: dict[str, Any], marked: bool
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        # A late delivery left an older target behind (ADR §6.2, reverse order).
        assert await _request_build(index, index.a, C3)
        await _unmark(index, index.a)

        assert await _reconcile(index, index.a, **head) is marked
        state = await _state(index, index.a)
        assert state is not None
        assert (state.target_commit_sha, state.indexed_commit_sha) == (head.get("commit", C1), C1)
        assert (await _marker(index, index.a) is not None) is marked

    asyncio.run(scenario())


@pytest.mark.integration
def test_reconcile_head_keeps_the_target_of_a_trigger_after_its_head_read(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        await _unmark(index, index.a)
        [(read_at,)] = await _rows(index, "SELECT clock_timestamp()")
        # A push lands after the reconcile read the head C2 and before its write.
        assert await _request_build(index, index.a, C3)
        pushed = await _marker(index, index.a)

        skipped = await _reconcile(index, index.a, commit=C2, read_at=read_at)
        kept = await _state(index, index.a)
        kept_marker = await _marker(index, index.a)
        # A marker set before the head read is no newer trigger: the read head wins.
        rewritten = await _reconcile(index, index.a, commit=C2)
        state = await _state(index, index.a)
        marker = await _marker(index, index.a)

        assert not skipped
        assert kept is not None and kept.target_commit_sha == C3
        assert kept_marker == pushed
        assert rewritten
        assert state is not None and state.target_commit_sha == C2
        assert marker is not None and pushed is not None and marker > pushed

    asyncio.run(scenario())


@pytest.mark.integration
def test_reconcile_head_judges_the_versions_of_the_locked_row(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        await _unmark(index, index.a)
        profiles: list[IndexProfile] = []

        def current(profile: IndexProfile) -> bool:
            profiles.append(profile)
            return True

        assert not await _reconcile(index, index.a, versions_current=current)
        assert profiles == [_PROFILE]
        assert await _marker(index, index.a) is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_reconcile_head_marks_an_index_without_a_publication(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _unmark(index, index.a)
        assert await _reconcile(index, index.a)
        state = await _state(index, index.a)
        assert state is not None
        assert (state.status, state.indexed_commit_sha, state.target_commit_sha) == (
            "empty",
            None,
            C1,
        )
        assert await _marker(index, index.a) is not None

    asyncio.run(scenario())


@pytest.mark.integration
def test_reconcile_head_leaves_another_generation_a_purged_and_a_failed_index_alone(
    index: _Index,
) -> None:
    async def scenario() -> None:
        assert not await _reconcile(index, index.a)
        await _enable(index, index.a)
        await _unmark(index, index.a)
        assert not await _reconcile(index, index.a, generation=2, commit=C2)
        state = await _state(index, index.a)
        assert state is not None and state.target_commit_sha is None
        assert await _marker(index, index.a) is None

        await _purge(index, index.a)
        assert not await _reconcile(index, index.a, generation=2, commit=C2)
        purged = await _state(index, index.a)
        assert purged is not None and purged.target_commit_sha is None

        await _enable(index, index.b)
        fence = await _claim(index, index.b)
        await _mark_failed(index, index.b, fence, "archive_unsafe")
        assert not await _reconcile(index, index.b, commit=C2)
        failed = await _state(index, index.b)
        assert failed is not None and failed.target_commit_sha is None
        assert await _marker(index, index.b) is None

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("code", "head", "marked"),
    [
        pytest.param("policy_document_too_large", {}, False, id="policy-same-head"),
        pytest.param("vcs_access_denied", {}, False, id="access-same-head"),
        pytest.param("internal_error", {}, False, id="internal-same-head"),
        # A cut or ambiguous listing of one SHA lists alike on every pass (#247).
        pytest.param(
            "policy_revision_unavailable", {}, False, id="policy-revision-unavailable-same-head"
        ),
        pytest.param("vcs_unavailable", {}, True, id="unavailable-same-head"),
        pytest.param("vcs_rate_limited", {}, True, id="rate-limited-same-head"),
        # A policy read that failed on the network heals like a VCS error (#242).
        pytest.param("policy_fetch_transient", {}, True, id="policy-transient-same-head"),
        pytest.param("policy_rate_limited", {}, True, id="policy-rate-limited-same-head"),
        pytest.param("policy_document_too_large", {"commit": C3}, True, id="policy-new-head"),
        pytest.param(
            "policy_document_too_large", {"branch": "develop"}, True, id="policy-new-branch"
        ),
        pytest.param(
            "policy_document_too_large",
            {"versions_current": lambda profile: False},
            True,
            id="policy-versions-outdated",
        ),
    ],
)
def test_reconcile_head_retries_a_terminal_refusal_only_on_a_new_head_branch_or_version(
    index: _Index, code: str, head: dict[str, Any], marked: bool
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        # A push to C2, whose job refused it: the publication of C1 stays.
        assert await _request_build(index, index.a, C2)
        fence = await _claim(index, index.a)
        assert await _release(index, index.a, fence, code) == IndexStatus.READY
        await _unmark(index, index.a)

        assert await _reconcile(index, index.a, **{"commit": C2, **head}) is marked
        state = await _state(index, index.a)
        assert state is not None
        assert (state.indexed_commit_sha, state.target_commit_sha, state.last_error_code) == (
            C1,
            head.get("commit", C2),
            # A new target clears the refusal of the older one.
            None if "commit" in head else code,
        )
        assert (await _marker(index, index.a) is not None) is marked

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("move", ["request", "reconcile"])
def test_a_new_target_clears_a_terminal_refusal_so_a_lost_wake_up_is_recovered(
    index: _Index, move: str
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        # A push to C2, whose job refused it: the publication of C1 stays.
        assert await _request_build(index, index.a, C2)
        fence = await _claim(index, index.a)
        assert await _release(index, index.a, fence, "policy_document_too_large") == (
            IndexStatus.READY
        )
        await _unmark(index, index.a)

        # The target moves to C3, by a trigger or by a pass, and its wake-up is lost.
        if move == "request":
            assert await _request_build(index, index.a, C3)
        else:
            assert await _reconcile(index, index.a, commit=C3)
        moved = await _state(index, index.a)
        await _unmark(index, index.a)

        assert await _reconcile(index, index.a, commit=C3)
        state = await _state(index, index.a)
        assert moved is not None and state is not None
        assert (moved.target_commit_sha, moved.last_error_code) == (C3, None)
        assert (state.indexed_commit_sha, state.target_commit_sha, state.last_error_code) == (
            C1,
            C3,
            None,
        )
        assert await _marker(index, index.a) is not None

    asyncio.run(scenario())


@pytest.mark.integration
def test_terminal_release_for_old_head_cannot_refuse_new_target(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        assert await _request_build(index, index.a, C2)
        fence = await _claim(index, index.a)
        assert await _request_build(index, index.a, C3)
        await _unmark(index, index.a)  # The newer target's wake-up was lost.

        async with index.uow() as uow:
            await uow.index.release(
                index.a,
                fence,
                last_error_code="vcs_access_denied",
                stats=IndexBuildStats(0, 0, 3, http_status=401),
                terminal_head_sha=C2,
                expected_target_sha=C2,
            )
            await uow.commit()
        after_release = await _state(index, index.a)
        assert after_release is not None
        assert (after_release.target_commit_sha, after_release.last_error_code) == (C3, None)
        assert after_release.last_error_http_status is None

        assert await _reconcile(index, index.a, commit=C3)
        assert await _marker(index, index.a) is not None

    asyncio.run(scenario())


@pytest.mark.integration
def test_successful_publish_clears_previous_http_status(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        assert await _request_build(index, index.a, C2)
        fence = await _claim(index, index.a)
        async with index.uow() as uow:
            await uow.index.release(
                index.a,
                fence,
                last_error_code="vcs_access_denied",
                stats=IndexBuildStats(0, 0, 3, http_status=401),
                terminal_head_sha=C2,
                expected_target_sha=C2,
            )
            await uow.commit()
        refused = await _state(index, index.a)
        assert refused is not None
        assert (refused.last_error_code, refused.last_error_http_status) == (
            "vcs_access_denied",
            401,
        )

        await _build(index, index.a, C2, _module("aa01", 1))
        published = await _state(index, index.a)
        assert published is not None
        assert (published.last_error_code, published.last_error_http_status) == (None, None)

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_write_of_the_same_target_keeps_a_terminal_refusal(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        assert await _request_build(index, index.a, C2)
        fence = await _claim(index, index.a)
        assert await _release(index, index.a, fence, "policy_document_too_large") == (
            IndexStatus.READY
        )
        await _unmark(index, index.a)

        # A repeated trigger of C2 and a trigger without a commit leave the target.
        assert await _request_build(index, index.a, C2)
        assert await _request_build(index, index.a, None)
        requested = await _state(index, index.a)
        await _unmark(index, index.a)
        marked = await _reconcile(index, index.a, commit=C2)
        state = await _state(index, index.a)

        assert requested is not None and state is not None
        assert (requested.target_commit_sha, requested.last_error_code) == (
            C2,
            "policy_document_too_large",
        )
        assert not marked
        assert (state.target_commit_sha, state.last_error_code) == (
            C2,
            "policy_document_too_large",
        )
        assert await _marker(index, index.a) is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_clear_wake_request_clears_only_the_marker_it_read(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        read = await _marker(index, index.a)
        assert read is not None
        # A trigger after the read sets a newer marker: the CAS misses and it stays.
        assert await _request_build(index, index.a, C2)
        newer = await _marker(index, index.a)
        assert not await _clear_wake(index, index.a, read)
        assert await _marker(index, index.a) == newer
        assert newer is not None and await _clear_wake(index, index.a, newer)
        assert await _marker(index, index.a) is None
        # Nothing left to clear; another repository's marker is untouched.
        await _enable(index, index.b)
        assert not await _clear_wake(index, index.a, newer)
        assert await _marker(index, index.b) is not None

    asyncio.run(scenario())
