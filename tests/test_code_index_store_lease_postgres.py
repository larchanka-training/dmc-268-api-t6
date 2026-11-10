"""PostgreSQL evidence for the store's fences, claims and leases (#141, ADR 0001 §6.2).

Opt-in: TEST_DATABASE_URL must target a test database whose user can create databases.
Each test gets a clone of a database migrated to head, dropped afterwards; the fixtures and the
store harness live in `tests/code_index_postgres.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from sqlalchemy import text

from app.modules.code_index.application.index_store import (
    IndexBuildStats,
    IndexLeaseLost,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.domain.enums import PurgeReason
from tests.code_index_postgres import (
    _ADMISSION,
    _COUNT_ROWS,
    _FILE_ROWS,
    _IMPORTER_BYTES,
    _LEASED,
    _MODULE_BYTES,
    _NO_STATS,
    _PROFILE,
    _PROFILE_FIELDS,
    _STALE,
    _STATS,
    C1,
    C2,
    C3,
    _build,
    _claim,
    _collect,
    _enable,
    _execute,
    _expire_lease,
    _fields,
    _importer,
    _Index,
    _module,
    _pause_after_lock,
    _publish,
    _purge,
    _read,
    _reads,
    _rows,
    _state,
    _try_claim,
    _wait_until_blocked,
    _write,
)

pytest_plugins = ("tests.code_index_postgres",)


@pytest.mark.integration
def test_enable_keeps_a_ready_index_and_disable_then_enable_rejects_the_old_fence(
    index: _Index,
) -> None:
    async def scenario() -> None:
        enabled = await _enable(index, index.a)
        assert _fields(enabled) == {
            "generation": 1,
            "status": "empty",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": None,
            "last_error_http_status": None,
            "indexed_commit_sha": None,
            "target_commit_sha": None,
            "reserved_publication": 0,
            "published_publication": 0,
            "floor_publication": 0,
            "file_count": 0,
            "symbol_count": 0,
            "edge_count": 0,
            "stored_bytes": 0,
            "pending_bytes": 0,
            "profile": None,
            "stats": _NO_STATS,
        }
        assert enabled.last_used_at is not None
        await _build(index, index.a, C1, _module("aa01", 1))
        ready = await _state(index, index.a)

        assert await _enable(index, index.a) == ready
        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) == _module(
            "aa01", 1
        )

        fence = await _claim(index, index.a)
        await _purge(index, index.a, PurgeReason.DISABLED)
        reenabled = await _enable(index, index.a)
        assert (
            reenabled.generation,
            reenabled.status,
            reenabled.purge_reason,
            reenabled.reserved_publication,
            reenabled.published_publication,
        ) == (3, "empty", None, 2, 0)
        assert reenabled.last_used_at is not None and reenabled.last_used_at > enabled.last_used_at
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _module("aa02", 1))
        assert await _try_claim(index, index.a, 1) == _STALE

    asyncio.run(scenario())


async def _purge_index(index: _Index, repository_id: UUID) -> None:
    await _purge(index, repository_id, PurgeReason.REMOVED)


async def _disable_and_enable(index: _Index, repository_id: UUID) -> None:
    await _purge(index, repository_id, PurgeReason.DISABLED)
    await _enable(index, repository_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("invalidate", "generation"),
    [
        pytest.param(_purge_index, 2, id="purge"),
        pytest.param(_disable_and_enable, 3, id="disable-enable"),
        pytest.param(_expire_lease, 1, id="expired-lease"),
    ],
)
def test_a_stale_fence_writes_and_publishes_nothing(
    index: _Index,
    invalidate: Callable[[_Index, UUID], Awaitable[None]],
    generation: int,
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _module("aa01", 1))
        await invalidate(index, index.a)
        stale = await _state(index, index.a)
        assert stale is not None and stale.generation == generation

        # Committed after each rejection: the check runs before any write of the same
        # transaction, not merely before the caller's rollback.
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _module("aa02", 1))
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _importer("ab01", 1))
            with pytest.raises(IndexLeaseLost):
                await uow.index.discard_abandoned(index.a, fence, 100)
            with pytest.raises(IndexLeaseLost):
                await uow.index.publish(
                    index.a,
                    fence,
                    base_commit_sha=None,
                    commit_sha=C1,
                    full=True,
                    deleted_paths=(),
                    profile=_PROFILE,
                    stats=_STATS,
                )
            with pytest.raises(IndexLeaseLost):
                await uow.index.mark_failed(index.a, fence, "limit_exceeded:symbols")
            with pytest.raises(IndexLeaseLost):
                await uow.index.release(index.a, fence, last_error_code="vcs_unavailable")
            await uow.commit()

        assert await _state(index, index.a) == stale
        assert await _rows(index, _FILE_ROWS, repository=index.a) == [(1, 1, None, "a.py", "aa01")]
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(1, 1, 0, 0)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_purge_waits_for_a_batch_write_and_then_rejects_the_old_fence(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        async with index.uow() as writer:
            await writer.index.replace_file(index.a, fence, _module("aa01", 1))
            writer_pid = await writer.session.scalar(text("SELECT pg_backend_pid()"))
            purge = asyncio.create_task(_purge(index, index.a))
            await _wait_until_blocked(index, writer_pid, 1, purge)
            await writer.commit()
        await asyncio.wait_for(purge, 5)

        purged = await _state(index, index.a)
        assert purged is not None and (purged.generation, purged.status) == (2, "empty")
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _importer("ab01", 1))
            await uow.commit()
        # The batch committed before the purge; its rows are of an old generation.
        assert await _rows(index, _FILE_ROWS, repository=index.a) == [(1, 1, None, "a.py", "aa01")]
        assert await _reads(index, index.a) == (None, [], [], [], [])

    asyncio.run(scenario())


@pytest.mark.integration
def test_two_batch_writes_of_one_lease_run_one_after_the_other(
    index: _Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)

        async def write_importer() -> IndexStorage:
            async with index.uow() as uow:
                storage = await uow.index.replace_file(index.a, fence, _importer("ab01", 1))
                await uow.commit()
            return storage

        async with index.uow() as first:
            first_pid = await first.session.scalar(text("SELECT pg_backend_pid()"))
            store = first.index
            locked, release = _pause_after_lock(monkeypatch, store)
            writing = asyncio.create_task(store.replace_file(index.a, fence, _module("aa01", 1)))
            await asyncio.wait_for(locked.wait(), 5)
            # Two writers sharing the state row would deadlock on the UPDATE of the pending
            # bytes; the second one queues behind the first on the admission lock, which
            # every batch takes before the row lock.
            second = asyncio.create_task(write_importer())
            await _wait_until_blocked(index, first_pid, 1, second)
            release.set()
            assert await writing == IndexStorage(
                stored_bytes=0, pending_bytes=_MODULE_BYTES, workspace_bytes=_MODULE_BYTES
            )
            await first.commit()

        assert await asyncio.wait_for(second, 5) == IndexStorage(
            stored_bytes=0,
            pending_bytes=_MODULE_BYTES + _IMPORTER_BYTES,
            workspace_bytes=_MODULE_BYTES + _IMPORTER_BYTES,
        )
        assert await _rows(index, _FILE_ROWS, repository=index.a) == [
            (1, 1, None, "a.py", "aa01"),
            (1, 1, None, "b.py", "ab01"),
        ]

    asyncio.run(scenario())


@pytest.mark.integration
def test_claim_takes_a_free_or_expired_lease_of_the_current_generation_only(
    index: _Index,
) -> None:
    async def scenario() -> None:
        assert await _try_claim(index, index.a, 1) == _STALE
        await _enable(index, index.a)
        assert await _try_claim(index, index.a, 2) == _STALE
        first = await _claim(index, index.a)
        assert (first.generation, first.publication) == (1, 1)
        assert await _try_claim(index, index.a, 1) == _LEASED
        building = await _state(index, index.a)
        assert building is not None
        assert (building.status, building.reserved_publication) == ("building", 1)
        [(remaining,)] = await _rows(
            index,
            "SELECT extract(epoch FROM lease_expires_at - clock_timestamp()) "
            "FROM code_index_states WHERE repository_id = :repository",
            repository=index.a,
        )
        assert 590 < remaining <= 600

        await _expire_lease(index, index.a)
        second = await _claim(index, index.a)
        assert (second.generation, second.publication) == (1, 2)
        assert second.token != first.token

        await _execute(
            index,
            "UPDATE code_index_states SET status = 'failed', error_code = 'archive_unsafe', "
            "lease_token = NULL, lease_expires_at = NULL, wake_requested_at = NULL "
            "WHERE repository_id = :repository",
            index.a,
        )
        failed = await _state(index, index.a)
        # ADR §7: a failed index is not rebuilt until the toggle is switched on again.
        assert await _try_claim(index, index.a, 1) == _STALE
        assert await _state(index, index.a) == failed
        assert failed is not None
        assert (failed.status, failed.error_code, failed.reserved_publication) == (
            "failed",
            "archive_unsafe",
            2,
        )

        await _purge(index, index.a)
        # Only enable re-arms a purged repository.
        assert await _try_claim(index, index.a, 2) == _STALE
        purged = await _state(index, index.a)
        assert purged is not None
        assert (purged.generation, purged.status, purged.reserved_publication) == (2, "empty", 2)
        await _enable(index, index.a)
        rearmed = await _claim(index, index.a, 3)
        assert (rearmed.generation, rearmed.publication) == (3, 3)

    asyncio.run(scenario())


@pytest.mark.integration
def test_publish_holds_the_state_row_for_update_against_toggle_and_gc(
    index: _Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _module("aa01", 1))
        async with index.uow() as publisher:
            publisher_pid = await publisher.session.scalar(text("SELECT pg_backend_pid()"))
            store = publisher.index
            locked, release = _pause_after_lock(monkeypatch, store)
            publishing = asyncio.create_task(
                store.publish(
                    index.a,
                    fence,
                    base_commit_sha=None,
                    commit_sha=C1,
                    full=True,
                    deleted_paths=(),
                    profile=_PROFILE,
                    stats=_STATS,
                )
            )
            await asyncio.wait_for(locked.wait(), 5)
            # FOR UPDATE blocks the collector's FOR SHARE; it is started first so that it
            # waits on the publisher itself, not in the queue behind the toggle.
            collecting = asyncio.create_task(_collect(index, index.a, 100))
            await _wait_until_blocked(index, publisher_pid, 1, collecting)
            toggling = asyncio.create_task(_disable_and_enable(index, index.a))
            await _wait_until_blocked(index, publisher_pid, 2, toggling, collecting)
            release.set()
            assert await publishing is True
            await publisher.commit()
        await asyncio.wait_for(asyncio.gather(toggling, collecting), 5)

        state = await _state(index, index.a)
        assert state is not None
        assert (
            state.generation,
            state.status,
            state.purge_reason,
            state.published_publication,
            state.indexed_commit_sha,
        ) == (3, "empty", None, 0, None)
        assert await _reads(index, index.a) == (None, [], [], [], [])
        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_duplicate_claim_waits_for_the_first_and_does_not_take_over(
    index: _Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        async with index.uow() as first:
            first_pid = await first.session.scalar(text("SELECT pg_backend_pid()"))
            store = first.index
            locked, release = _pause_after_lock(monkeypatch, store)
            claiming = asyncio.create_task(store.claim_lease(index.a, 1, 600, _ADMISSION))
            await asyncio.wait_for(locked.wait(), 5)
            duplicate = asyncio.create_task(_try_claim(index, index.a, 1))
            await _wait_until_blocked(index, first_pid, 1, duplicate)
            release.set()
            claim = await claiming
            await first.commit()

        assert await asyncio.wait_for(duplicate, 5) == _LEASED
        assert isinstance(claim, LeaseClaim)
        assert (claim.fence.generation, claim.fence.publication) == (1, 1)
        state = await _state(index, index.a)
        assert state is not None
        assert (state.status, state.reserved_publication) == ("building", 1)

    asyncio.run(scenario())


@pytest.mark.integration
def test_lease_expiry_is_checked_against_the_clock_not_the_transaction_start(
    index: _Index,
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        async with index.uow() as uow:
            claim = await uow.index.claim_lease(index.a, 1, 1, _ADMISSION)
            await uow.commit()
        assert isinstance(claim, LeaseClaim)
        fence = claim.fence
        async with index.uow() as uow:
            started = await uow.session.scalar(text("SELECT now()"))
            state = await uow.index.get_state(index.a)
            # By the transaction's start time the lease is still live.
            assert state is not None and state.lease_expires_at is not None
            assert state.lease_expires_at > started
            await asyncio.sleep(1.2)
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _module("aa01", 1))
            await uow.commit()

        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(0, 0, 0, 0)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_publication_miss_spends_the_lease_so_the_next_wake_up_claims_at_once(
    index: _Index,
) -> None:
    missed_stats = IndexBuildStats(
        downloaded_bytes=81_920, build_duration_ms=42_000, github_request_count=4
    )

    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _module("aa02", 1))
        # The build started from C2, but the published index is at C1.
        assert not await _publish(
            index, index.a, fence, C3, base=C2, full=False, stats=missed_stats
        )

        missed = await _state(index, index.a)
        assert _fields(missed) == {
            "generation": 1,
            "status": "ready",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": None,
            "last_error_http_status": None,
            "indexed_commit_sha": C1,
            "target_commit_sha": None,
            "reserved_publication": 2,
            "published_publication": 1,
            "floor_publication": 1,
            "file_count": 1,
            "symbol_count": 1,
            "edge_count": 0,
            # The miss also drops the attempt's pending bytes.
            "stored_bytes": _MODULE_BYTES,
            "pending_bytes": 0,
            "profile": _PROFILE_FIELDS,
            # The missed job's metrics, as a release records them; the counters above
            # stay those of the visible publication.
            "stats": {
                "downloaded_bytes": 81_920,
                "build_duration_ms": 42_000,
                "github_request_count": 4,
                "http_status": None,
            },
        }
        assert missed is not None and missed.lease_expires_at is None
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _module("aa03", 1))
            await uow.commit()
        assert (await _claim(index, index.a)).publication == 3
        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) == _module(
            "aa01", 1
        )

    asyncio.run(scenario())
