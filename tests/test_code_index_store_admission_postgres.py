"""PostgreSQL evidence for the indexer's store (#142): admission, failure, release and
byte accounting.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create databases.
Each test gets a clone of a database migrated to head, dropped afterwards; the fixtures and the
store harness live in `tests/code_index_postgres.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from app.modules.code_index.application.index_store import (
    ClaimRefusal,
    ClaimRefused,
    ImportRecord,
    IndexBuildStats,
    IndexedFile,
    IndexFence,
    IndexLeaseLost,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.domain.storage import COLUMN_WEIGHTS, ROW_OVERHEAD_BYTES, row_bytes
from app.modules.reviews.application.ast_resolution import ImportResolution
from tests.code_index_postgres import (
    _A,
    _ADMISSION,
    _COUNT_ROWS,
    _E,
    _IMPORTER_BYTES,
    _LEASED,
    _MODULE_BYTES,
    _NO_STATS,
    _PROFILE,
    _PROFILE_FIELDS,
    _STALE,
    _STATS,
    _STATS_FIELDS,
    _WIDE,
    C1,
    C2,
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
    _read,
    _reads,
    _release,
    _repository,
    _rows,
    _sibling,
    _state,
    _symbol,
    _try_claim,
    _uses,
    _wait_until_blocked,
    _write,
)

pytest_plugins = ("tests.code_index_postgres",)


_BUSY = ClaimRefused(ClaimRefusal.BUSY)


async def _wait_until_lease_expired(index: _Index, repository_id: UUID) -> None:
    """Wait until the committed lease of `repository_id` is past the database clock."""
    async with asyncio.timeout(10), index.factory() as observer:
        while not await observer.scalar(
            text(
                "SELECT lease_expires_at <= clock_timestamp() FROM code_index_states "
                "WHERE repository_id = :repository"
            ),
            {"repository": repository_id},
        ):
            await asyncio.sleep(0.05)


@pytest.mark.integration
def test_claim_is_busy_at_two_live_leases_or_one_per_installation(
    index: _Index, code_index_database: Connection
) -> None:
    # A and A2 share an installation; B and C each have a Workspace of their own.
    a2 = _sibling(code_index_database, index.a, external_id=102)
    c = UUID(_repository(code_index_database, installation_external_id=19))
    code_index_database.commit()

    async def scenario() -> None:
        for repository_id in (index.a, a2, index.b, c):
            await _enable(index, repository_id)
        await _claim(index, index.a)
        assert await _try_claim(index, a2, 1) == _BUSY
        await _claim(index, index.b)
        assert await _try_claim(index, c, 1) == _BUSY
        # STALE and LEASED are decided before BUSY, and STALE before LEASED.
        assert await _try_claim(index, index.a, 1) == _LEASED
        assert await _try_claim(index, index.a, 2) == _STALE
        assert await _try_claim(index, c, 2) == _STALE
        assert await _try_claim(index, uuid4(), 1) == _STALE
        refused = await _state(index, c)
        assert refused is not None
        assert (refused.status, refused.reserved_publication, refused.lease_expires_at) == (
            "empty",
            0,
            None,
        )

        # An expired lease counts neither for its installation nor in the total.
        await _expire_lease(index, index.a)
        assert (await _claim(index, a2)).publication == 1
        assert await _try_claim(index, c, 1) == _BUSY
        await _expire_lease(index, index.b)
        assert (await _claim(index, c)).publication == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_concurrent_claims_wait_for_the_admission_lock(
    index: _Index, code_index_database: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a2 = _sibling(code_index_database, index.a, external_id=102)

    async def scenario() -> None:
        for repository_id in (index.a, a2):
            await _enable(index, repository_id)
        async with index.uow() as first:
            first_pid = await first.session.scalar(text("SELECT pg_backend_pid()"))
            store = first.index
            locked, release = _pause_after_lock(monkeypatch, store)
            claiming = asyncio.create_task(store.claim_lease(index.a, 1, 600, _ADMISSION))
            await asyncio.wait_for(locked.wait(), 5)
            # Another state row: only the admission lock can hold this claim back.
            sibling = asyncio.create_task(_try_claim(index, a2, 1))
            await _wait_until_blocked(index, first_pid, 1, sibling)
            release.set()
            assert isinstance(await claiming, LeaseClaim)
            await first.commit()

        # It counted the committed lease of its installation instead of racing it.
        assert await asyncio.wait_for(sibling, 5) == _BUSY

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_claim_waits_for_a_purge_without_a_deadlock(index: _Index) -> None:
    async def scenario() -> None:
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
        async with index.uow() as purger:
            await purger.index.purge(index.a, PurgeReason.DISABLED)
            purger_pid = await purger.session.scalar(text("SELECT pg_backend_pid()"))
            # A's claim holds the admission lock while it waits for A's state row; B's
            # claim queues behind it on the admission lock.
            claim_a = asyncio.create_task(_try_claim(index, index.a, 1))
            await _wait_until_blocked(index, purger_pid, 1, claim_a)
            claim_b = asyncio.create_task(_try_claim(index, index.b, 1))
            await _wait_until_blocked(index, purger_pid, 2, claim_a, claim_b)
            await purger.commit()

        assert await asyncio.wait_for(claim_a, 5) == _STALE
        granted = await asyncio.wait_for(claim_b, 5)
        assert isinstance(granted, LeaseClaim) and granted.fence.publication == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_mark_failed_drops_the_publication_and_gc_removes_every_row(index: _Index) -> None:
    async def scenario() -> None:
        failed_stats = IndexBuildStats(
            downloaded_bytes=1_024, build_duration_ms=900_000, github_request_count=4
        )
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
        # A is published and carries an earlier error code; B was never published.
        await _build(index, index.a, C1, _module("aa01", 1), _importer("ab01", 1))
        assert await _release(index, index.a, await _claim(index, index.a), "vcs_unavailable") == (
            "ready"
        )
        fence_a = await _claim(index, index.a)
        await _write(index, index.a, fence_a, IndexedFile("c.py", "ac03"))
        fence_b = await _claim(index, index.b)
        await _write(index, index.b, fence_b, _module("ba01", 1))

        building = await _state(index, index.a)
        async with index.uow() as uow:
            with pytest.raises(ValueError, match="'vcs_unavailable' is not an error code"):
                await uow.index.mark_failed(index.a, fence_a, "vcs_unavailable")
            await uow.commit()
        assert await _state(index, index.a) == building

        await _mark_failed(index, index.a, fence_a, "limit_exceeded:symbols")
        await _mark_failed(index, index.b, fence_b, "archive_unsafe", failed_stats)

        failed_a = await _state(index, index.a)
        assert _fields(failed_a) == {
            "generation": 1,
            "status": "failed",
            "purge_reason": None,
            "error_code": "limit_exceeded:symbols",
            "last_error_code": "vcs_unavailable",
            "last_error_http_status": None,
            "indexed_commit_sha": None,
            "target_commit_sha": None,
            "reserved_publication": 3,
            "published_publication": 0,
            "floor_publication": 0,
            "file_count": 0,
            "symbol_count": 0,
            "edge_count": 0,
            "stored_bytes": 0,
            "pending_bytes": 0,
            "profile": None,
            # Without stats a failure keeps the recorded ones.
            "stats": _STATS_FIELDS,
        }
        assert failed_a is not None
        assert (failed_a.published_at, failed_a.lease_expires_at) == (None, None)
        assert _fields(await _state(index, index.b)) == {
            "generation": 1,
            "status": "failed",
            "purge_reason": None,
            "error_code": "archive_unsafe",
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
            "stats": {
                "downloaded_bytes": 1_024,
                "build_duration_ms": 900_000,
                "github_request_count": 4,
                "http_status": None,
            },
        }
        assert await _reads(index, index.a) == (None, [], [], [], [])
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence_a, _module("aa04", 1))
            with pytest.raises(IndexLeaseLost):
                await uow.index.mark_failed(index.a, fence_a, "limit_exceeded:edges")
            with pytest.raises(IndexLeaseLost):
                await uow.index.release(index.a, fence_a)

        # A: publication 1 (8 rows) and the failed attempt's file; B: a file and a symbol.
        assert await _collect(index, index.a, 100) == [9, 0]
        assert await _collect(index, index.b, 100) == [2, 0]
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(0, 0, 0, 0)]
        assert await _try_claim(index, index.a, 1) == _STALE

    asyncio.run(scenario())


@pytest.mark.integration
def test_release_keeps_the_last_publication_and_records_the_last_error_code(
    index: _Index,
) -> None:
    async def scenario() -> None:
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
        # B was never published: a release returns it to empty.
        fence = await _claim(index, index.b)
        await _write(index, index.b, fence, _module("ba01", 1))
        assert await _release(index, index.b, fence, "vcs_unavailable") == "empty"
        released = await _state(index, index.b)
        assert _fields(released) == {
            "generation": 1,
            "status": "empty",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": "vcs_unavailable",
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
        assert released is not None and released.lease_expires_at is None
        assert await _collect(index, index.b, 100) == [2, 0]
        assert await _release(index, index.b, await _claim(index, index.b)) == "empty"
        kept = await _state(index, index.b)
        assert kept is not None
        assert (kept.status, kept.last_error_code) == ("empty", "vcs_unavailable")

        # A keeps its publication readable through a rebuild that gave up.
        await _build(index, index.a, C1, _module("aa01", 1))
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _module("aa02", 1))
        assert await _release(index, index.a, fence, "vcs_access_denied") == "ready"
        stale = await _state(index, index.a)
        assert stale is not None
        assert (
            stale.status,
            stale.last_error_code,
            stale.indexed_commit_sha,
            stale.published_publication,
            stale.stored_bytes,
            stale.pending_bytes,
            stale.lease_expires_at,
        ) == ("ready", "vcs_access_denied", C1, 1, _MODULE_BYTES, 0, None)
        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) == _module(
            "aa01", 1
        )
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.release(index.a, fence)
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _module("aa03", 1))
        assert await _release(index, index.a, await _claim(index, index.a)) == "ready"
        kept = await _state(index, index.a)
        assert kept is not None and kept.last_error_code == "vcs_access_denied"

        # A publication clears it.
        await _build(index, index.a, C2, _module("aa04", 1), base=C1)
        published = await _state(index, index.a)
        assert published is not None
        assert (published.indexed_commit_sha, published.last_error_code) == (C2, None)

    asyncio.run(scenario())


@pytest.mark.integration
def test_release_records_the_job_metrics_and_keeps_the_publication_counters(
    index: _Index,
) -> None:
    # A job that ends without a publication records its metrics too; the counters and
    # stored bytes stay those of the visible publication.
    stats = IndexBuildStats(
        downloaded_bytes=2_048, build_duration_ms=30_000, github_request_count=2
    )
    stats_fields = {
        "downloaded_bytes": 2_048,
        "build_duration_ms": 30_000,
        "github_request_count": 2,
        "http_status": None,
    }

    async def scenario() -> None:
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
        # A is published with `_STATS`; its rebuild gives up after writing a file.
        await _build(index, index.a, C1, _module("aa01", 1), _importer("ab01", 1))
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _module("aa02", 1))
        assert await _release(index, index.a, fence, "vcs_unavailable", stats) == "ready"
        assert _fields(await _state(index, index.a)) == {
            "generation": 1,
            "status": "ready",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": "vcs_unavailable",
            "last_error_http_status": None,
            "indexed_commit_sha": C1,
            "target_commit_sha": None,
            "reserved_publication": 2,
            "published_publication": 1,
            "floor_publication": 1,
            "file_count": 2,
            "symbol_count": 2,
            "edge_count": 2,
            "stored_bytes": _MODULE_BYTES + _IMPORTER_BYTES,
            "pending_bytes": 0,
            "profile": _PROFILE_FIELDS,
            "stats": stats_fields,
        }

        # B was never published: it returns to empty with zero counters and the metrics.
        fence = await _claim(index, index.b)
        await _write(index, index.b, fence, _module("ba01", 1))
        assert await _release(index, index.b, fence, "vcs_access_denied", stats) == "empty"
        assert _fields(await _state(index, index.b)) == {
            "generation": 1,
            "status": "empty",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": "vcs_access_denied",
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
            "stats": stats_fields,
        }

        # Without stats a release keeps the recorded metrics.
        for repository_id, status in ((index.a, "ready"), (index.b, "empty")):
            assert await _release(index, repository_id, await _claim(index, repository_id)) == (
                status
            )
            kept = await _state(index, repository_id)
            assert kept is not None and dataclasses.asdict(kept.stats) == stats_fields

    asyncio.run(scenario())


@pytest.mark.integration
def test_estimate_weights_count_the_indexes_on_each_text_column(
    code_index_database: Connection,
) -> None:
    # w(c) = 1 + the number of indexes, the primary key included, that contain column c.
    weights: dict[str, dict[str, int]] = {}
    for table, column, indexes in code_index_database.execute(
        text(
            "SELECT c.relname, a.attname, count(i.indexrelid) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "LEFT JOIN pg_index i ON i.indrelid = a.attrelid AND a.attnum = ANY(i.indkey) "
            "WHERE c.relnamespace = current_schema()::regnamespace "
            "AND c.relname IN ('code_index_files', 'code_index_symbols', 'code_index_edges', "
            "'code_index_imports') AND a.attnum > 0 AND NOT a.attisdropped "
            "AND a.atttypid = 'character varying'::regtype GROUP BY 1, 2"
        )
    ):
        weights.setdefault(table, {})[column] = 1 + indexes
    assert weights == {
        "code_index_files": {"path": 2, "blob_sha": 1},
        "code_index_symbols": {"path": 2, "qualified_name": 2},
        "code_index_edges": {"path": 2, "target_path": 3, "target_name": 3},
        "code_index_imports": {"path": 2, "specifier": 2, "target_path": 2},
    }
    assert weights == COLUMN_WEIGHTS


@pytest.mark.integration
def test_pending_bytes_count_a_rewritten_file_once_and_publish_stores_them(
    index: _Index,
) -> None:
    path = f"{_E}.py"  # 5 UTF-8 bytes
    wide = IndexedFile(
        path,
        "b1",
        symbols=(_symbol(path, _A, 1, 1),),
        # A duplicate edge collapses onto one row and is counted once.
        edges=(_uses(path, _WIDE, _A), _uses(path, _WIDE, _A)),
        imports=(
            ImportRecord(path, _WIDE, ImportResolution.RESOLVED, _E),
            ImportRecord(path, "x", ImportResolution.MISSING, None),
        ),
    )
    # The Python half: file 160+2·5+2, symbol 160+2·5+2·2, edge 160+2·5+3·4+3·2, imports
    # 160+2·5+2·4+2·2 and 160+2·5+2·1.
    assert ROW_OVERHEAD_BYTES == 160
    assert [
        row_bytes("code_index_files", {"path": path, "blob_sha": "b1"}),
        row_bytes("code_index_symbols", {"path": path, "qualified_name": _A}),
        row_bytes("code_index_edges", {"path": path, "target_path": _WIDE, "target_name": _A}),
        row_bytes("code_index_imports", {"path": path, "specifier": _WIDE, "target_path": _E}),
        row_bytes("code_index_imports", {"path": path, "specifier": "x", "target_path": None}),
    ] == [172, 174, 188, 182, 172]
    wide_bytes = 888

    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        storages: list[IndexStorage] = []
        for file in (wide, wide, IndexedFile(path, "b2"), wide, _module("aa01", 1)):
            async with index.uow() as uow:
                storages.append(await uow.index.replace_file(index.a, fence, file))
                await uow.commit()
        # A rewrite first subtracts the SQL estimate of the rows it deletes, so the
        # Python estimate of the new rows is never counted on top of the old ones. A is
        # alone in its Workspace, so the Workspace holds its stored and pending bytes.
        assert storages == [
            IndexStorage(stored_bytes=0, pending_bytes=wide_bytes, workspace_bytes=wide_bytes),
            IndexStorage(stored_bytes=0, pending_bytes=wide_bytes, workspace_bytes=wide_bytes),
            IndexStorage(stored_bytes=0, pending_bytes=172, workspace_bytes=172),
            IndexStorage(stored_bytes=0, pending_bytes=wide_bytes, workspace_bytes=wide_bytes),
            IndexStorage(
                stored_bytes=0,
                pending_bytes=wide_bytes + _MODULE_BYTES,
                workspace_bytes=wide_bytes + _MODULE_BYTES,
            ),
        ]
        # The SQL half over the published rows equals the Python half of the writes.
        assert await _publish(index, index.a, fence, C1)
        full = await _state(index, index.a)
        assert full is not None
        assert (full.stored_bytes, full.pending_bytes) == (wide_bytes + _MODULE_BYTES, 0)

        fence = await _claim(index, index.a)
        rewritten = IndexedFile(
            "a.py", "aa02", symbols=(_symbol("a.py", "foo", 3, 4), _symbol("a.py", "qux", 6, 7))
        )
        async with index.uow() as uow:
            assert await uow.index.replace_file(index.a, fence, rewritten) == IndexStorage(
                stored_bytes=wide_bytes + _MODULE_BYTES,
                pending_bytes=520,
                workspace_bytes=wide_bytes + _MODULE_BYTES + 520,
            )
            await uow.commit()
        assert await _publish(index, index.a, fence, C2, base=C1, full=False)
        incremental = await _state(index, index.a)
        assert incremental is not None
        assert (incremental.stored_bytes, incremental.pending_bytes) == (wide_bytes + 520, 0)

    asyncio.run(scenario())


@pytest.mark.integration
def test_repository_quota_refuses_the_claim_and_fails_the_index(index: _Index) -> None:
    async def scenario() -> None:
        stored = _MODULE_BYTES + _IMPORTER_BYTES
        for repository_id, prefix in ((index.a, "a"), (index.b, "b")):
            await _enable(index, repository_id)
            await _build(
                index, repository_id, C1, _module(f"{prefix}a01", 1), _importer(f"{prefix}b01", 1)
            )
        await _execute(
            index,
            f"UPDATE code_index_states SET target_commit_sha = '{C2}' "
            "WHERE repository_id = :repository",
            index.a,
        )
        # A's Workspace holds A's bytes only, so a claim at both quotas is granted.
        at_quota = dataclasses.replace(
            _ADMISSION, repository_storage_bytes=stored, workspace_storage_bytes=stored
        )
        over_quota = dataclasses.replace(_ADMISSION, repository_storage_bytes=stored - 1)

        claim = await _try_claim(index, index.a, 1, at_quota)
        assert isinstance(claim, LeaseClaim)
        # The target snapshot is taken with the lease.
        assert (claim.fence.publication, claim.target_commit_sha) == (2, C2)
        held = await _state(index, index.a)
        # LEASED is decided before QUOTA, so a live lease is never failed by a quota.
        assert await _try_claim(index, index.a, 1, over_quota) == _LEASED
        assert await _state(index, index.a) == held
        assert held is not None and (held.status, held.error_code) == ("ready", None)
        assert await _release(index, index.a, claim.fence) == "ready"

        assert await _try_claim(index, index.a, 1, over_quota) == ClaimRefused(
            ClaimRefusal.QUOTA, "limit_exceeded:repository_storage"
        )
        assert _fields(await _state(index, index.a)) == {
            "generation": 1,
            "status": "failed",
            "purge_reason": None,
            "error_code": "limit_exceeded:repository_storage",
            "last_error_code": None,
            "last_error_http_status": None,
            "indexed_commit_sha": None,
            "target_commit_sha": C2,
            "reserved_publication": 2,
            "published_publication": 0,
            "floor_publication": 0,
            "file_count": 0,
            "symbol_count": 0,
            "edge_count": 0,
            "stored_bytes": 0,
            "pending_bytes": 0,
            "profile": None,
            # The refused job made no GitHub request: it records zero metrics, not the
            # previous job's.
            "stats": _NO_STATS,
        }
        assert await _reads(index, index.a) == (None, [], [], [], [])
        assert await _collect(index, index.a, 100) == [8, 0]
        # The quota is per repository: B holds as many bytes and keeps its index.
        b = await _state(index, index.b)
        assert b is not None and (b.status, b.stored_bytes) == ("ready", stored)
        assert await _rows(index, _COUNT_ROWS, repository=index.b) == [(2, 2, 2, 2)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_workspace_quota_counts_other_builds_and_spares_another_workspace(
    index: _Index, code_index_database: Connection
) -> None:
    # S shares A's Workspace through another installation, so it never makes A busy.
    s = _sibling(code_index_database, index.a, external_id=101, installation_external_id=19)

    async def scenario() -> None:
        for repository_id in (index.a, s, index.b):
            await _enable(index, repository_id)
        await _build(index, index.a, C1, _module("aa01", 1))
        await _build(index, index.b, C1, _module("ba01", 1))
        building = await _try_claim(index, s, 1)
        assert isinstance(building, LeaseClaim)
        # A's stored bytes and the pending bytes of S's build.
        workspace = _MODULE_BYTES + _IMPORTER_BYTES
        assert await _write(index, s, building.fence, _importer("sb01", 1)) == [
            IndexStorage(stored_bytes=0, pending_bytes=_IMPORTER_BYTES, workspace_bytes=workspace)
        ]
        at_quota = dataclasses.replace(_ADMISSION, workspace_storage_bytes=workspace)
        over_quota = dataclasses.replace(_ADMISSION, workspace_storage_bytes=workspace - 1)

        claim = await _try_claim(index, index.a, 1, at_quota)
        assert isinstance(claim, LeaseClaim)
        assert await _release(index, index.a, claim.fence) == "ready"
        other = await _try_claim(index, index.b, 1, over_quota)
        assert isinstance(other, LeaseClaim)
        # B's Workspace counts neither A's stored bytes nor S's pending ones.
        assert await _write(index, index.b, other.fence, IndexedFile("c.py", "bc01")) == [
            IndexStorage(
                stored_bytes=_MODULE_BYTES, pending_bytes=172, workspace_bytes=_MODULE_BYTES + 172
            )
        ]
        # S and B now hold both live leases: QUOTA is decided before BUSY.
        # A ready repository fails too; its publication is no longer used.
        assert await _try_claim(index, index.a, 1, over_quota) == ClaimRefused(
            ClaimRefusal.QUOTA, "limit_exceeded:workspace_storage"
        )
        failed = await _state(index, index.a)
        assert failed is not None
        assert (
            failed.status,
            failed.error_code,
            failed.published_publication,
            failed.stored_bytes,
        ) == ("failed", "limit_exceeded:workspace_storage", 0, 0)
        # A's earlier build recorded `_STATS`; the refused job made no GitHub request.
        assert dataclasses.asdict(failed.stats) == _NO_STATS

        # The build that filled the Workspace keeps running.
        running = await _state(index, s)
        assert running is not None
        assert (running.status, running.pending_bytes) == ("building", _IMPORTER_BYTES)

    asyncio.run(scenario())


@pytest.mark.integration
def test_workspace_quota_skips_the_pending_bytes_of_an_expired_lease(
    index: _Index, code_index_database: Connection
) -> None:
    s = _sibling(code_index_database, index.a, external_id=101, installation_external_id=19)

    async def scenario() -> None:
        for repository_id in (index.a, s):
            await _enable(index, repository_id)
        await _build(index, index.a, C1, _module("aa01", 1))
        abandoned = await _claim(index, s)
        await _write(index, s, abandoned, _importer("sb01", 1))
        # S's worker died: its pending bytes stay on the row until S is claimed again.
        await _expire_lease(index, s)
        only_a = dataclasses.replace(_ADMISSION, workspace_storage_bytes=_MODULE_BYTES)

        claim = await _try_claim(index, index.a, 1, only_a)
        assert isinstance(claim, LeaseClaim)
        # A write skips them too: A's stored bytes and its own pending ones.
        assert await _write(index, index.a, claim.fence, IndexedFile("c.py", "ac02")) == [
            IndexStorage(
                stored_bytes=_MODULE_BYTES, pending_bytes=172, workspace_bytes=_MODULE_BYTES + 172
            )
        ]
        stale = await _state(index, s)
        assert stale is not None
        assert (stale.status, stale.pending_bytes) == ("building", _IMPORTER_BYTES)

    asyncio.run(scenario())


class _OverQuota(Exception):
    """What the build raises on a batch over a storage quota, before its commit."""


@pytest.mark.integration
def test_a_batch_counts_the_pending_bytes_of_other_builds_against_the_workspace_quota(
    index: _Index, code_index_database: Connection
) -> None:
    # The #159 review case: 512 B per repository, 2,048 B per Workspace. Four published
    # repositories of A's Workspace hold 4 · 346 = 1,384 B; X and Y, in two installations
    # of it, build 346 B each, so each claim is granted on 1,384 B.
    published = [
        index.a,
        *(_sibling(code_index_database, index.a, external_id=n) for n in (102, 103, 104)),
    ]
    x = _sibling(code_index_database, index.a, external_id=105)
    y = _sibling(code_index_database, index.a, external_id=101, installation_external_id=19)
    quota = dataclasses.replace(
        _ADMISSION, repository_storage_bytes=512, workspace_storage_bytes=2_048
    )

    async def scenario() -> None:
        for repository_id in (*published, x, y):
            await _enable(index, repository_id)
        for number, repository_id in enumerate(published):
            await _build(index, repository_id, C1, _module(f"aa0{number}", 1))
        fence_x = await _claim(index, x, admission=quota)
        fence_y = await _claim(index, y, admission=quota)

        assert await _write(index, x, fence_x, _module("xa01", 1)) == [
            IndexStorage(stored_bytes=0, pending_bytes=346, workspace_bytes=1_730)
        ]
        # The store only measures; the caller refuses. Y is within its own quota, but
        # with X's committed batch the Workspace holds 2,076 B, so the caller raises
        # before its commit and the batch rolls back.
        with pytest.raises(_OverQuota):
            async with index.uow() as uow:
                storage = await uow.index.replace_file(y, fence_y, _module("ya01", 1))
                assert storage == IndexStorage(
                    stored_bytes=0, pending_bytes=346, workspace_bytes=2_076
                )
                assert storage.stored_bytes + storage.pending_bytes <= (
                    quota.repository_storage_bytes
                )
                assert storage.workspace_bytes > quota.workspace_storage_bytes
                raise _OverQuota
        # The rolled-back batch left neither rows nor pending bytes.
        rolled_back = await _state(index, y)
        assert rolled_back is not None and rolled_back.pending_bytes == 0
        assert await _rows(index, _COUNT_ROWS, repository=y) == [(0, 0, 0, 0)]
        # X rewrites its file: the Workspace is back at 1,730 B, without Y's 346 B, and
        # every committed state stays within the repository quota.
        assert await _write(index, x, fence_x, _module("xa02", 1)) == [
            IndexStorage(stored_bytes=0, pending_bytes=346, workspace_bytes=1_730)
        ]

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_batch_and_a_claim_wait_for_an_open_batch_and_then_count_its_bytes(
    index: _Index, code_index_database: Connection
) -> None:
    # S shares A's Workspace through another installation; B is in a Workspace of its own.
    s = _sibling(code_index_database, index.a, external_id=101, installation_external_id=19)

    async def scenario() -> None:
        for repository_id in (index.a, s, index.b):
            await _enable(index, repository_id)
        fence_a = await _claim(index, index.a)
        fence_s = await _claim(index, s)
        async with index.uow() as writer:
            assert await writer.index.replace_file(
                index.a, fence_a, _module("aa01", 1)
            ) == IndexStorage(
                stored_bytes=0, pending_bytes=_MODULE_BYTES, workspace_bytes=_MODULE_BYTES
            )
            writer_pid = await writer.session.scalar(text("SELECT pg_backend_pid()"))
            # Other state rows, and B even another Workspace: only the admission lock,
            # held until A's batch commits, holds them back.
            batch = asyncio.create_task(_write(index, s, fence_s, _importer("sb01", 1)))
            claim = asyncio.create_task(_try_claim(index, index.b, 1))
            await _wait_until_blocked(index, writer_pid, 2, batch, claim)
            await writer.commit()

        # S's batch read A's pending bytes after A's commit.
        assert await asyncio.wait_for(batch, 5) == [
            IndexStorage(
                stored_bytes=0,
                pending_bytes=_IMPORTER_BYTES,
                workspace_bytes=_MODULE_BYTES + _IMPORTER_BYTES,
            )
        ]
        # The claim counted the two live leases of A and S.
        assert await asyncio.wait_for(claim, 5) == _BUSY

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_batch_waits_for_an_open_publication_and_counts_its_stored_bytes(
    index: _Index, code_index_database: Connection
) -> None:
    # The #159 review case, round 2: 512 B per repository, 2,048 B per Workspace; four
    # published repositories of A's Workspace hold 1,384 B. X's lease expires while its
    # publication of 346 B is open. A batch of Y, in another installation, that did not
    # wait would count neither X's pending bytes (expired lease) nor its stored ones
    # (uncommitted): 1,730 B, and X's commit would then make it 2,076 B.
    published = [
        index.a,
        *(_sibling(code_index_database, index.a, external_id=n) for n in (102, 103, 104)),
    ]
    x = _sibling(code_index_database, index.a, external_id=105)
    y = _sibling(code_index_database, index.a, external_id=101, installation_external_id=19)
    quota = dataclasses.replace(
        _ADMISSION, repository_storage_bytes=512, workspace_storage_bytes=2_048
    )

    async def measure_y(fence: IndexFence) -> IndexStorage:
        async with index.uow() as uow:
            # The store only measures; the build refuses a batch over a quota before its
            # commit, so this one is never committed.
            return await uow.index.replace_file(y, fence, _module("ya01", 1))

    async def scenario() -> None:
        for repository_id in (*published, x, y):
            await _enable(index, repository_id)
        for number, repository_id in enumerate(published):
            await _build(index, repository_id, C1, _module(f"aa0{number}", 1))
        fence_y = await _claim(index, y, admission=quota)
        async with index.uow() as uow:
            claim = await uow.index.claim_lease(x, 1, 3, quota)
            await uow.commit()
        assert isinstance(claim, LeaseClaim)
        assert await _write(index, x, claim.fence, _module("xa01", 1)) == [
            IndexStorage(stored_bytes=0, pending_bytes=346, workspace_bytes=1_730)
        ]

        async with index.uow() as publisher:
            assert await publisher.index.publish(
                x,
                claim.fence,
                base_commit_sha=None,
                commit_sha=C1,
                full=True,
                deleted_paths=(),
                profile=_PROFILE,
                stats=_STATS,
            )
            publisher_pid = await publisher.session.scalar(text("SELECT pg_backend_pid()"))
            await _wait_until_lease_expired(index, x)
            batch = asyncio.create_task(measure_y(fence_y))
            await _wait_until_blocked(index, publisher_pid, 1, batch)
            await publisher.commit()

        # Y's batch read X's committed publication: over the Workspace quota.
        assert await asyncio.wait_for(batch, 5) == IndexStorage(
            stored_bytes=0, pending_bytes=346, workspace_bytes=2_076
        )
        published_x = await _state(index, x)
        assert published_x is not None
        assert (published_x.status, published_x.stored_bytes) == ("ready", 346)

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_duplicate_claim_waits_for_an_open_publication_without_a_deadlock(
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
            # A duplicate claim of the same repository takes the admission lock first and
            # then wants the state row; a publication that took the row first would meet
            # it in a lock-order cycle.
            duplicate = asyncio.create_task(_try_claim(index, index.a, 1))
            await _wait_until_blocked(index, publisher_pid, 1, duplicate)
            release.set()
            assert await publishing is True
            await publisher.commit()

        # The publication spent the lease, so the claim takes the next number.
        granted = await asyncio.wait_for(duplicate, 5)
        assert isinstance(granted, LeaseClaim) and granted.fence.publication == 2

    asyncio.run(scenario())
