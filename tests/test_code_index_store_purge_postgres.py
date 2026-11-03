"""PostgreSQL evidence for purge, garbage collection and the TTL purge (#141, #143).

The TTL purge (ADR §4 lifetime row) takes any live index except `failed`.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards; the fixtures and the
store harness live in `tests/code_index_postgres.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.modules.code_index.application.index_store import (
    IndexedFile,
    IndexFence,
    IndexLeaseLost,
    IndexState,
)
from app.modules.code_index.domain.enums import PurgeReason
from tests.code_index_postgres import (
    _COUNT_ROWS,
    _FILE_ROWS,
    _NO_STATS,
    _TTL,
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
    _mark_failed,
    _module,
    _pause_after_lock,
    _publish,
    _purge,
    _purge_unused,
    _read,
    _reads,
    _rows,
    _state,
    _unused_for_31_days,
    _wait_until_blocked,
    _write,
)

pytest_plugins = ("tests.code_index_postgres",)


@pytest.mark.integration
def test_purge_hides_every_row_gc_deletes_them_and_a_repeated_purge_is_a_no_op(
    index: _Index,
) -> None:
    async def scenario() -> None:
        for repository_id, prefix in ((index.a, "a"), (index.b, "b")):
            await _enable(index, repository_id)
            await _build(
                index, repository_id, C1, _module(f"{prefix}a01", 1), _importer(f"{prefix}b01", 1)
            )
        enabled = await _state(index, index.a)
        reads_b = await _reads(index, index.b)
        await _purge(index, index.a, PurgeReason.DISABLED)

        purged = await _state(index, index.a)
        assert _fields(purged) == {
            "generation": 2,
            "status": "empty",
            "purge_reason": "disabled",
            "error_code": None,
            "last_error_code": None,
            "last_error_http_status": None,
            "indexed_commit_sha": None,
            "target_commit_sha": None,
            "reserved_publication": 1,
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
        assert purged is not None and enabled is not None
        assert (purged.published_at, purged.lease_expires_at) == (None, None)
        assert purged.last_used_at == enabled.last_used_at
        assert await _reads(index, index.a) == (None, [], [], [], [])

        await _purge(index, index.a, PurgeReason.TTL)
        assert await _state(index, index.a) == purged

        # 2 files, 2 symbols, 2 edges and 2 imports, deleted in portions of 3.
        assert await _collect(index, index.a, 3) == [3, 3, 2, 0]
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(0, 0, 0, 0)]
        assert await _state(index, index.a) == purged
        assert await _rows(index, _COUNT_ROWS, repository=index.b) == [(2, 2, 2, 2)]
        assert await _reads(index, index.b) == reads_b

    asyncio.run(scenario())


@pytest.mark.integration
def test_incremental_publication_waits_until_abandoned_rows_are_discarded(index: _Index) -> None:
    async def scenario() -> None:
        lost: dict[UUID, IndexFence] = {}
        for repository_id, prefix in ((index.a, "a"), (index.b, "b")):
            await _enable(index, repository_id)
            await _build(index, repository_id, C1, _module(f"{prefix}a01", 1))
            lost[repository_id] = await _claim(index, repository_id)
            # Publication 2 writes 6 rows: a file, a symbol, two edges and two imports.
            await _write(index, repository_id, lost[repository_id], _importer(f"{prefix}b02", 1))
        await _expire_lease(index, index.a)
        missed = await _claim(index, index.a)
        assert missed.publication == 3
        await _write(index, index.a, missed, IndexedFile("c.py", "ac03"))

        # Publication 2's rows would turn visible once `published` passed them.
        assert not await _publish(index, index.a, missed, C3, base=C1, full=False)
        async with index.uow() as uow:
            for stale in (lost[index.a], missed):
                with pytest.raises(IndexLeaseLost):
                    await uow.index.discard_abandoned(index.a, stale, 100)

        # The miss spent the lease: the next wake-up claims at once and discards first.
        fence = await _claim(index, index.a)
        assert fence.publication == 4
        discarded: list[int] = []
        while not discarded or discarded[-1]:
            async with index.uow() as uow:
                discarded.append(await uow.index.discard_abandoned(index.a, fence, 4))
                await uow.commit()
        # Publication 2's six rows and publication 3's file row.
        assert discarded == [4, 3, 0]
        assert await _rows(index, _FILE_ROWS, repository=index.a) == [(1, 1, None, "a.py", "aa01")]
        assert await _rows(index, _COUNT_ROWS, repository=index.b) == [(2, 2, 2, 2)]

        await _write(index, index.a, fence, IndexedFile("c.py", "ac04"))
        assert await _publish(index, index.a, fence, C3, base=C1, full=False)
        published = await _state(index, index.a)
        assert published is not None
        assert (
            published.published_publication,
            published.floor_publication,
            published.indexed_commit_sha,
            published.file_count,
            published.symbol_count,
            published.edge_count,
        ) == (4, 1, C3, 2, 1, 0)
        assert await _read(index, lambda store: store.get_file(index.a, "b.py")) is None
        assert await _read(index, lambda store: store.get_file(index.a, "c.py")) == IndexedFile(
            "c.py", "ac04"
        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_gc_spares_the_lease_and_waits_ten_minutes_for_displaced_rows(index: _Index) -> None:
    async def scenario() -> None:
        async def revoke() -> bool:
            async with index.uow() as uow:
                revoked = await uow.index.revoke_expired_lease(index.a)
                await uow.commit()
            return revoked

        for repository_id, prefix in ((index.a, "a"), (index.b, "b")):
            await _enable(index, repository_id)
            await _build(
                index,
                repository_id,
                C1,
                _module(f"{prefix}a01", 1),
                IndexedFile("b.py", f"{prefix}b01"),
            )
        # A full build: its floor hides publication 1 without closing its rows.
        await _build(index, index.a, C2, _module("aa02", 1), base=C1)
        abandoned = await _claim(index, index.a)
        await _write(index, index.a, abandoned, IndexedFile("c.py", "ac03"))

        assert await revoke() is False
        assert await _collect(index, index.a, 10) == [0]
        await _expire_lease(index, index.a)
        # An expired lease still protects its rows until it is revoked.
        assert await _collect(index, index.a, 10) == [0]
        expired = await _state(index, index.a)
        # c.py/ac03: 160 + 2·4 + 4.
        assert expired is not None and expired.pending_bytes == 172
        assert await revoke() is True
        revoked = await _state(index, index.a)
        assert revoked is not None and revoked.pending_bytes == 0
        assert await revoke() is False
        live = await _claim(index, index.a)
        assert live.publication == 4
        await _write(index, index.a, live, IndexedFile("d.py", "ad04"))

        assert await _collect(index, index.a, 10) == [1, 0]
        assert await _rows(index, _FILE_ROWS, repository=index.a) == [
            (1, 1, None, "a.py", "aa01"),
            (1, 1, None, "b.py", "ab01"),
            (1, 2, None, "a.py", "aa02"),
            (1, 4, None, "d.py", "ad04"),
        ]
        await _execute(
            index,
            "UPDATE code_index_states SET published_at = published_at - interval '11 minutes' "
            "WHERE repository_id = :repository",
            index.a,
        )
        # Publication 1: two files and a symbol.
        assert await _collect(index, index.a, 10) == [3, 0]
        assert await _rows(index, _FILE_ROWS, repository=index.a) == [
            (1, 2, None, "a.py", "aa02"),
            (1, 4, None, "d.py", "ad04"),
        ]
        assert await _rows(index, _FILE_ROWS, repository=index.b) == [
            (1, 1, None, "a.py", "ba01"),
            (1, 1, None, "b.py", "bb01"),
        ]
        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) == _module(
            "aa02", 1
        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_later_purge_replaces_only_a_ttl_reason(index: _Index) -> None:
    async def scenario() -> None:
        expired: dict[UUID, IndexState] = {}
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
            await _build(index, repository_id, C1, _module("aa01", 1))
            await _purge(index, repository_id, PurgeReason.TTL)
            state = await _state(index, repository_id)
            assert state is not None
            assert (state.generation, state.purge_reason) == (2, "ttl")
            expired[repository_id] = state

        async def purge(repository_id: UUID, reason: PurgeReason) -> IndexState:
            await _purge(index, repository_id, reason)
            state = await _state(index, repository_id)
            assert state is not None
            return state

        removed = await purge(index.a, PurgeReason.REMOVED)
        assert (removed.generation, removed.purge_reason) == (2, "removed")
        assert (
            dataclasses.replace(
                removed, purge_reason=PurgeReason.TTL, updated_at=expired[index.a].updated_at
            )
            == expired[index.a]
        )
        assert await purge(index.a, PurgeReason.DISABLED) == removed
        assert await purge(index.a, PurgeReason.TTL) == removed

        disabled = await purge(index.b, PurgeReason.DISABLED)
        assert (disabled.generation, disabled.purge_reason) == (2, "disabled")
        assert (
            dataclasses.replace(
                disabled, purge_reason=PurgeReason.TTL, updated_at=expired[index.b].updated_at
            )
            == expired[index.b]
        )
        assert await purge(index.b, PurgeReason.REMOVED) == disabled
        assert await purge(index.b, PurgeReason.TTL) == disabled

    asyncio.run(scenario())


@pytest.mark.integration
def test_purge_unused_purges_an_old_live_index_with_the_ttl_reason(index: _Index) -> None:
    async def scenario() -> None:
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
            await _build(index, repository_id, C1, _module("aa01", 1))
        await _unused_for_31_days(index, index.a)
        old = await _state(index, index.a)
        untouched = await _state(index, index.b)

        assert await _purge_unused(index, index.a) is True

        purged = await _state(index, index.a)
        assert purged is not None and old is not None
        assert (
            purged.generation,
            purged.status,
            purged.purge_reason,
            purged.published_publication,
            purged.file_count,
        ) == (2, "empty", "ttl", 0, 0)
        assert purged.last_used_at == old.last_used_at
        assert await _reads(index, index.a) == (None, [], [], [], [])
        assert await _state(index, index.b) == untouched
        assert await _purge_unused(index, index.a) is False
        assert await _state(index, index.a) == purged

    asyncio.run(scenario())


@pytest.mark.integration
def test_purge_unused_leaves_a_failed_a_purged_and_a_fresh_index_alone(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        fresh = await _state(index, index.a)
        assert await _purge_unused(index, index.a) is False
        assert await _state(index, index.a) == fresh
        # A NULL last_used_at is not older than the TTL either.
        await _execute(
            index,
            "UPDATE code_index_states SET last_used_at = NULL WHERE repository_id = :repository",
            index.a,
        )
        never_used = await _state(index, index.a)
        assert await _purge_unused(index, index.a) is False
        assert await _state(index, index.a) == never_used

        fence = await _claim(index, index.a, generation=1)
        await _mark_failed(index, index.a, fence, "limit_exceeded:symbols")
        await _unused_for_31_days(index, index.a)
        failed = await _state(index, index.a)
        assert failed is not None and failed.status == "failed"
        assert await _purge_unused(index, index.a) is False
        assert await _state(index, index.a) == failed

        await _enable(index, index.b)
        await _purge(index, index.b, PurgeReason.DISABLED)
        await _unused_for_31_days(index, index.b)
        disabled = await _state(index, index.b)
        assert await _purge_unused(index, index.b) is False
        assert await _state(index, index.b) == disabled

        assert await _purge_unused(index, uuid4()) is False

    asyncio.run(scenario())


@pytest.mark.integration
def test_purge_unused_rechecks_last_used_at_after_the_row_lock(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        await _unused_for_31_days(index, index.a)
        async with index.factory() as holder:
            await holder.execute(
                text(
                    "SELECT 1 FROM code_index_states WHERE repository_id = :repository FOR UPDATE"
                ),
                {"repository": index.a},
            )
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            purging = asyncio.create_task(_purge_unused(index, index.a))
            await _wait_until_blocked(index, holder_pid, 1, purging)
            # A use of the index refreshes last_used_at while the purge waits for the row.
            await holder.execute(
                text(
                    "UPDATE code_index_states SET last_used_at = clock_timestamp() "
                    "WHERE repository_id = :repository"
                ),
                {"repository": index.a},
            )
            await holder.commit()

        assert await asyncio.wait_for(purging, 5) is False
        state = await _state(index, index.a)
        assert state is not None
        assert (state.generation, state.status, state.purge_reason) == (1, "ready", None)

    asyncio.run(scenario())


@pytest.mark.integration
def test_two_parallel_ttl_purges_purge_the_index_once(
    index: _Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        await _unused_for_31_days(index, index.a)
        async with index.uow() as first:
            first_pid = await first.session.scalar(text("SELECT pg_backend_pid()"))
            store = first.index
            locked, release = _pause_after_lock(monkeypatch, store)
            first_run = asyncio.create_task(store.purge_unused(index.a, _TTL))
            await asyncio.wait_for(locked.wait(), 5)
            second_run = asyncio.create_task(_purge_unused(index, index.a))
            await _wait_until_blocked(index, first_pid, 1, second_run)
            release.set()
            assert await first_run is True
            await first.commit()

        assert await asyncio.wait_for(second_run, 5) is False
        state = await _state(index, index.a)
        assert state is not None
        assert (state.generation, state.status, state.purge_reason) == (2, "empty", "ttl")

    asyncio.run(scenario())
