"""PostgreSQL evidence for the store's file writes, publications and reads (#141, ADR 0001
§6.2): keys, row checks before a write, isolation by `repository_id`.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards; the fixtures and the
store harness live in `tests/code_index_postgres.py`.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.modules.code_index.application.index_store import (
    EdgeTarget,
    ImportRecord,
    IndexCounts,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
    IndexLeaseLost,
    LeaseClaim,
)
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.code_index.domain.keys import (
    MAX_INDEX_KEY_BYTES,
    SymbolKey,
    assign_disambiguators,
)
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import SymbolKind
from tests.code_index_postgres import (
    _A,
    _ADMISSION,
    _COUNT_ROWS,
    _E,
    _FILE_ROWS,
    _IMPORTER_BYTES,
    _MODULE_BYTES,
    _PROFILE,
    _PROFILE_FIELDS,
    _STATS,
    _STATS_FIELDS,
    _WIDE,
    C1,
    C2,
    _build,
    _claim,
    _enable,
    _fields,
    _importer,
    _imports,
    _Index,
    _module,
    _publish,
    _purge,
    _read,
    _reads,
    _rows,
    _state,
    _symbol,
    _uses,
    _write,
)

pytest_plugins = ("tests.code_index_postgres",)


@pytest.mark.integration
def test_store_flushes_and_only_the_callers_commit_persists(index: _Index) -> None:
    async def scenario() -> None:
        async with index.uow() as uow:
            await uow.index.enable(index.a)
            claim = await uow.index.claim_lease(index.a, 1, 600, _ADMISSION)
            assert isinstance(claim, LeaseClaim)
            await uow.index.replace_file(index.a, claim.fence, _module("aa01", 1))
            own = await uow.session.execute(text(_COUNT_ROWS), {"repository": index.a})
            # The rows reached the caller's transaction, and no other session sees them.
            assert tuple(own.one()) == (1, 1, 0, 0)
            assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(0, 0, 0, 0)]
        assert await _state(index, index.a) is None
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(0, 0, 0, 0)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_replacing_a_file_twice_in_one_attempt_leaves_only_the_second_version(
    index: _Index,
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        second = IndexedFile("b.py", "ab02", symbols=(_symbol("b.py", "baz", 3, 4),))
        await _write(index, index.a, fence, _module("aa01", 1), _importer("ab01", 1), second)
        assert await _publish(index, index.a, fence, C1)

        assert await _read(index, lambda store: store.get_file(index.a, "b.py")) == second
        # The first version's symbol, edges and imports are deleted, not only hidden.
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(2, 2, 0, 0)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_publications_record_literal_state_and_incremental_closes_rewritten_files(
    index: _Index,
) -> None:
    async def scenario() -> None:
        enabled = await _enable(index, index.a)
        fence = await _claim(index, index.a)
        assert (fence.generation, fence.publication) == (1, 1)
        building = await _state(index, index.a)
        assert building is not None
        assert (building.status, building.reserved_publication) == ("building", 1)
        await _write(index, index.a, fence, _module("aa01", 1), _importer("ab01", 1))
        async with index.uow() as uow:
            published_at = await uow.session.scalar(text("SELECT now()"))
            assert await uow.index.publish(
                index.a,
                fence,
                base_commit_sha=None,
                commit_sha=C1,
                full=True,
                deleted_paths=(),
                profile=_PROFILE,
                stats=_STATS,
            )
            await uow.commit()

        state = await _state(index, index.a)
        assert _fields(state) == {
            "generation": 1,
            "status": "ready",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": None,
            "last_error_http_status": None,
            "indexed_commit_sha": C1,
            "target_commit_sha": None,
            "reserved_publication": 1,
            "published_publication": 1,
            "floor_publication": 1,
            "file_count": 2,
            "symbol_count": 2,
            "edge_count": 2,
            "stored_bytes": _MODULE_BYTES + _IMPORTER_BYTES,
            "pending_bytes": 0,
            "profile": _PROFILE_FIELDS,
            "stats": _STATS_FIELDS,
        }
        assert state is not None
        assert (state.published_at, state.last_used_at) == (published_at, enabled.last_used_at)
        # Publication spends the lease: the fence can no longer write visible rows.
        assert state.lease_expires_at is None
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _module("aa0x", 1))

        fence = await _claim(index, index.a)
        rewritten = IndexedFile(
            "a.py", "aa02", symbols=(_symbol("a.py", "foo", 3, 4), _symbol("a.py", "qux", 6, 7))
        )
        await _write(index, index.a, fence, rewritten)
        assert await _publish(index, index.a, fence, C2, base=C1, full=False)

        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) == rewritten
        assert await _read(index, lambda store: store.get_file(index.a, "b.py")) == _importer(
            "ab01", 1
        )
        assert _fields(await _state(index, index.a)) == {
            "generation": 1,
            "status": "ready",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": None,
            "last_error_http_status": None,
            "indexed_commit_sha": C2,
            "target_commit_sha": None,
            "reserved_publication": 2,
            "published_publication": 2,
            "floor_publication": 1,
            "file_count": 2,
            "symbol_count": 3,
            "edge_count": 2,
            # The rewritten a.py (172 + foo 174 + qux 174) and the untouched b.py.
            "stored_bytes": 520 + _IMPORTER_BYTES,
            "pending_bytes": 0,
            "profile": _PROFILE_FIELDS,
            "stats": _STATS_FIELDS,
        }
        assert await _rows(index, _FILE_ROWS, repository=index.a) == [
            (1, 1, 2, "a.py", "aa01"),
            (1, 1, None, "b.py", "ab01"),
            (1, 2, None, "a.py", "aa02"),
        ]

    asyncio.run(scenario())


@pytest.mark.integration
def test_every_read_for_one_repository_returns_only_its_own_rows(index: _Index) -> None:
    async def scenario() -> None:
        for repository_id, prefix, start in ((index.a, "a", 1), (index.b, "b", 11)):
            await _enable(index, repository_id)
            await _build(
                index,
                repository_id,
                C1,
                _module(f"{prefix}a01", start),
                _importer(f"{prefix}b01", start),
            )

        assert await _reads(index, index.a) == (
            IndexedFile("a.py", "aa01", symbols=(_symbol("a.py", "foo", 1, 2),)),
            [_symbol("b.py", "Bar", 1, 5, SymbolKind.CLASS)],
            [
                EdgeTarget(_imports("b.py", "a.py"), "aa01", ()),
                EdgeTarget(_uses("b.py", "a.py", "foo"), "aa01", (_symbol("a.py", "foo", 1, 2),)),
            ],
            [_uses("b.py", "a.py", "foo")],
            [ImportRecord("b.py", ".a", ImportResolution.RESOLVED, "a.py")],
        )
        assert await _reads(index, index.b) == (
            IndexedFile("a.py", "ba01", symbols=(_symbol("a.py", "foo", 11, 12),)),
            [_symbol("b.py", "Bar", 11, 15, SymbolKind.CLASS)],
            [
                EdgeTarget(_imports("b.py", "a.py"), "ba01", ()),
                EdgeTarget(_uses("b.py", "a.py", "foo"), "ba01", (_symbol("a.py", "foo", 11, 12),)),
            ],
            [_uses("b.py", "a.py", "foo")],
            [ImportRecord("b.py", ".a", ImportResolution.RESOLVED, "a.py")],
        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_writes_for_one_repository_leave_another_repositorys_rows_alone(index: _Index) -> None:
    async def scenario() -> None:
        for repository_id, prefix in ((index.a, "a"), (index.b, "b")):
            await _enable(index, repository_id)
            await _build(index, repository_id, C1, _module(f"{prefix}a01", 1))
        fence_a = await _claim(index, index.a)
        fence_b = await _claim(index, index.b)
        # Same generation, publication number and path in both repositories.
        assert (fence_a.generation, fence_a.publication) == (1, 2)
        assert (fence_b.generation, fence_b.publication) == (1, 2)
        await _write(index, index.b, fence_b, _module("ba02", 1))
        await _write(index, index.a, fence_a, _module("aa0x", 1), _module("aa02", 1))
        assert await _publish(index, index.a, fence_a, C2, base=C1, full=False)

        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) == _module(
            "aa02", 1
        )
        assert await _read(index, lambda store: store.get_file(index.b, "a.py")) == _module(
            "ba01", 1
        )
        assert await _rows(index, _FILE_ROWS, repository=index.b) == [
            (1, 1, None, "a.py", "ba01"),
            (1, 2, None, "a.py", "ba02"),
        ]
        assert await _publish(index, index.b, fence_b, C2, base=C1, full=False)
        assert await _read(index, lambda store: store.get_file(index.b, "a.py")) == _module(
            "ba02", 1
        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_symbol_keys_survive_reordering_and_a_new_commit_and_the_edge_resolves(
    index: _Index,
) -> None:
    async def scenario() -> None:
        function = SymbolKind.FUNCTION
        foo = SymbolKey("a.py", "foo", function, 0)
        bar = SymbolKey("a.py", "bar", function, 0)
        baz = SymbolKey("a.py", "baz", function, 0)
        importer = IndexedFile(
            "b.py",
            "ab01",
            edges=(
                _imports("b.py", "a.py"),
                _uses("b.py", "a.py", "bar"),
                _uses("b.py", "a.py", "foo"),
            ),
        )
        c1 = assign_disambiguators("a.py", [("foo", function), ("bar", function)])
        await _enable(index, index.a)
        await _build(
            index,
            index.a,
            C1,
            IndexedFile(
                "a.py", "aa01", symbols=(IndexedSymbol(c1[0], 1, 2), IndexedSymbol(c1[1], 4, 5))
            ),
            importer,
        )
        assert await _read(index, lambda store: store.outgoing_edges(index.a, "b.py")) == [
            EdgeTarget(_imports("b.py", "a.py"), "aa01", ()),
            EdgeTarget(_uses("b.py", "a.py", "bar"), "aa01", (IndexedSymbol(bar, 4, 5),)),
            EdgeTarget(_uses("b.py", "a.py", "foo"), "aa01", (IndexedSymbol(foo, 1, 2),)),
        ]

        c2 = assign_disambiguators(
            "a.py", [("bar", function), ("baz", function), ("foo", function)]
        )
        symbols = (
            IndexedSymbol(c2[0], 1, 2),
            IndexedSymbol(c2[1], 4, 5),
            IndexedSymbol(c2[2], 7, 8),
        )
        await _build(
            index, index.a, C2, IndexedFile("a.py", "aa02", symbols=symbols), base=C1, full=False
        )

        assert await _read(index, lambda store: store.file_symbols(index.a, "a.py")) == [
            IndexedSymbol(bar, 1, 2),
            IndexedSymbol(baz, 4, 5),
            IndexedSymbol(foo, 7, 8),
        ]
        assert await _read(index, lambda store: store.outgoing_edges(index.a, "b.py")) == [
            EdgeTarget(_imports("b.py", "a.py"), "aa02", ()),
            EdgeTarget(_uses("b.py", "a.py", "bar"), "aa02", (IndexedSymbol(bar, 1, 2),)),
            EdgeTarget(_uses("b.py", "a.py", "foo"), "aa02", (IndexedSymbol(foo, 7, 8),)),
        ]
        assert await _read(index, lambda store: store.incoming_edges(index.a, "a.py", "foo")) == [
            _uses("b.py", "a.py", "foo")
        ]
        # The stored key carries neither the commit nor the declaration's position.
        assert await _rows(
            index,
            "SELECT valid_from, qualified_name, kind::text, disambiguator, start_line "
            "FROM code_index_symbols WHERE repository_id = :repository "
            "ORDER BY valid_from, start_line",
            repository=index.a,
        ) == [
            (1, "foo", "function", 0, 1),
            (1, "bar", "function", 0, 4),
            (2, "bar", "function", 0, 1),
            (2, "baz", "function", 0, 4),
            (2, "foo", "function", 0, 7),
        ]

    asyncio.run(scenario())


@pytest.mark.integration
def test_deleting_the_target_file_leaves_the_edge_unresolved(index: _Index) -> None:
    async def scenario() -> None:
        importer = IndexedFile(
            "b.py",
            "ab01",
            edges=(_imports("b.py", "a.py"), _uses("b.py", "a.py", "foo")),
            imports=(ImportRecord("b.py", ".a", ImportResolution.RESOLVED, "a.py"),),
        )
        newcomer = IndexedFile("c.py", "ac02", symbols=(_symbol("c.py", "foo", 1, 2),))
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1), importer)
        await _build(index, index.a, C2, newcomer, base=C1, full=False, deleted=("a.py",))

        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) is None
        assert await _read(index, lambda store: store.outgoing_edges(index.a, "b.py")) == [
            EdgeTarget(_imports("b.py", "a.py"), None, ()),
            EdgeTarget(_uses("b.py", "a.py", "foo"), None, ()),
        ]
        # The same name in another file is not a target.
        assert await _read(index, lambda store: store.incoming_edges(index.a, "c.py", "foo")) == []
        assert await _read(index, lambda store: store.file_symbols(index.a, "c.py")) == [
            _symbol("c.py", "foo", 1, 2)
        ]
        state = await _state(index, index.a)
        assert state is not None
        assert (state.file_count, state.symbol_count, state.edge_count) == (2, 1, 2)

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("accepted", "rejected"),
    [
        pytest.param(
            IndexedFile(_WIDE * 500, "f1"), IndexedFile(_WIDE * 500 + "x", "f2"), id="file"
        ),
        pytest.param(
            IndexedFile(_E * 998, "s1", symbols=(_symbol(_E * 998, "abcd", 1, 1),)),
            IndexedFile(_E * 998, "s2", symbols=(_symbol(_E * 998, "abcde", 1, 1),)),
            id="symbol",
        ),
        pytest.param(
            IndexedFile("b.py", "e1", edges=(_uses("b.py", _E * 498, _A * 500),)),
            IndexedFile("b.py", "e2", edges=(_uses("b.py", _E * 498 + "x", _A * 500),)),
            id="edge",
        ),
        pytest.param(
            IndexedFile(
                "b.py",
                "i1",
                imports=(ImportRecord("b.py", _E * 998, ImportResolution.MISSING, None),),
            ),
            IndexedFile(
                "b.py",
                "i2",
                imports=(ImportRecord("b.py", _E * 998 + "x", ImportResolution.MISSING, None),),
            ),
            id="import",
        ),
        pytest.param(
            IndexedFile(
                "b.py",
                "t1",
                imports=(ImportRecord("b.py", "pkg", ImportResolution.RESOLVED, _WIDE * 500),),
            ),
            IndexedFile(
                "b.py",
                "t2",
                imports=(
                    ImportRecord("b.py", "pkg", ImportResolution.RESOLVED, _WIDE * 500 + "x"),
                ),
            ),
            id="import-target",
        ),
    ],
)
def test_replace_file_rejects_a_key_over_the_btree_bound_before_any_write(
    index: _Index, accepted: IndexedFile, rejected: IndexedFile
) -> None:
    # Each accepted key is exactly 2000 UTF-8 bytes; each rejected key is one byte more.
    assert MAX_INDEX_KEY_BYTES == 2000

    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, accepted)
        async with index.uow() as uow:
            with pytest.raises(ValueError, match="2001 UTF-8 bytes"):
                await uow.index.replace_file(index.a, fence, rejected)
            await uow.commit()
        assert await _publish(index, index.a, fence, C1)

        assert await _rows(
            index,
            "SELECT path, blob_sha FROM code_index_files WHERE repository_id = :repository",
            repository=index.a,
        ) == [(accepted.path, accepted.blob_sha)]
        assert await _read(index, lambda store: store.get_file(index.a, accepted.path)) == accepted

    asyncio.run(scenario())


@pytest.mark.integration
def test_replace_file_rejects_rows_that_belong_to_another_path(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        stray = IndexedFile("a.py", "aa01", symbols=(_symbol("b.py", "foo", 1, 2),))
        async with index.uow() as uow:
            with pytest.raises(ValueError, match="must carry the file's path"):
                await uow.index.replace_file(index.a, fence, stray)
            await uow.commit()

        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(0, 0, 0, 0)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_replace_file_collapses_duplicate_edges_and_imports_onto_their_keys(
    index: _Index,
) -> None:
    async def scenario() -> None:
        resolved = ImportRecord("b.py", ".a", ImportResolution.RESOLVED, "a.py")
        dynamic = ImportRecord("b.py", ".a", ImportResolution.DYNAMIC, None)
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        await _write(
            index,
            index.a,
            fence,
            IndexedFile(
                "b.py",
                "ab01",
                edges=(_uses("b.py", "a.py", "foo"), _uses("b.py", "a.py", "foo")),
                imports=(resolved, resolved, dynamic),
            ),
        )
        assert await _publish(index, index.a, fence, C1)

        # A static and a dynamic import of one specifier are two records.
        assert await _read(index, lambda store: store.get_file(index.a, "b.py")) == IndexedFile(
            "b.py", "ab01", edges=(_uses("b.py", "a.py", "foo"),), imports=(resolved, dynamic)
        )
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(1, 0, 1, 2)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_replace_file_writes_more_rows_than_one_insert_statement_can_bind(index: _Index) -> None:
    # One multi-row INSERT binds 9 values per symbol and 7 per edge or import, so under
    # PostgreSQL's 65,535 parameters it tops out at 7,281 symbols and 9,362 edges or imports.
    large = IndexedFile(
        "a.py",
        "aa01",
        symbols=tuple(_symbol("a.py", f"s{line:05d}", line, line) for line in range(1, 8_001)),
        edges=tuple(_uses("a.py", "b.py", f"n{number:05d}") for number in range(9_400)),
        imports=tuple(
            ImportRecord("a.py", f"m{number:05d}", ImportResolution.EXTERNAL, None)
            for number in range(9_400)
        ),
    )

    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, large)

        state = await _state(index, index.a)
        assert state is not None
        assert (state.file_count, state.symbol_count, state.edge_count) == (1, 8_000, 9_400)
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(1, 8_000, 9_400, 9_400)]
        assert await _read(index, lambda store: store.get_file(index.a, "a.py")) == large

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        pytest.param(
            IndexedFile("p" * 1025, "f1"),
            "code_index_files.path is 1025 characters, over its limit of 1024",
            id="path",
        ),
        pytest.param(
            IndexedFile("a.py", "f" * 65),
            "code_index_files.blob_sha is 65 characters, over its limit of 64",
            id="blob-sha",
        ),
        pytest.param(
            IndexedFile("a.py", "s1", symbols=(_symbol("a.py", "q" * 513, 1, 1),)),
            "code_index_symbols.qualified_name is 513 characters, over its limit of 512",
            id="qualified-name",
        ),
        pytest.param(
            IndexedFile("a.py", "e1", edges=(_uses("a.py", "t" * 1025, "foo"),)),
            "code_index_edges.target_path is 1025 characters, over its limit of 1024",
            id="edge-target-path",
        ),
        pytest.param(
            IndexedFile("a.py", "e2", edges=(_uses("a.py", "b.py", "n" * 513),)),
            "code_index_edges.target_name is 513 characters, over its limit of 512",
            id="target-name",
        ),
        pytest.param(
            IndexedFile(
                "a.py",
                "i1",
                imports=(ImportRecord("a.py", "m" * 1025, ImportResolution.MISSING, None),),
            ),
            "code_index_imports.specifier is 1025 characters, over its limit of 1024",
            id="specifier",
        ),
        pytest.param(
            IndexedFile(
                "a.py",
                "i2",
                imports=(ImportRecord("a.py", "m", ImportResolution.RESOLVED, "t" * 1025),),
            ),
            "code_index_imports.target_path is 1025 characters, over its limit of 1024",
            id="import-target-path",
        ),
        pytest.param(
            IndexedFile(
                "a.py", "d1", symbols=(_symbol("a.py", "foo", 1, 2), _symbol("a.py", "foo", 4, 5))
            ),
            "duplicate symbol key",
            id="duplicate-symbol-key",
        ),
        pytest.param(
            IndexedFile(
                "a.py", "k1", edges=(IndexedEdge("a.py", EdgeKind.IMPORTS, "b.py", "foo"),)
            ),
            "only an imports edge has an empty target_name",
            id="imports-edge-with-name",
        ),
        pytest.param(
            IndexedFile(
                "a.py", "k2", edges=(IndexedEdge("a.py", EdgeKind.USES_IMPORTED, "b.py", ""),)
            ),
            "only an imports edge has an empty target_name",
            id="uses-edge-without-name",
        ),
    ],
)
def test_replace_file_rejects_rows_the_schema_would_refuse_before_any_write(
    index: _Index, invalid: IndexedFile, message: str
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _module("aa01", 1))
        async with index.uow() as uow:
            with pytest.raises(ValueError, match=message):
                await uow.index.replace_file(index.a, fence, invalid)
            await uow.commit()

        assert await _rows(index, _FILE_ROWS, repository=index.a) == [(1, 1, None, "a.py", "aa01")]
        assert await _rows(index, _COUNT_ROWS, repository=index.a) == [(1, 1, 0, 0)]

    asyncio.run(scenario())


@pytest.mark.integration
def test_incremental_reads_page_through_the_visible_files_and_imports(index: _Index) -> None:
    resolved = ImportRecord("c.py", ".a", ImportResolution.RESOLVED, "a.py")
    dynamic = ImportRecord("c.py", ".a", ImportResolution.DYNAMIC, None)

    async def scenario() -> None:
        for repository_id in (index.a, index.b):
            await _enable(index, repository_id)
        await _build(
            index,
            index.a,
            C1,
            _module("aa01", 1),
            _importer("ab01", 1),
            IndexedFile("c.py", "ac01", imports=(resolved, dynamic)),
        )
        await _build(index, index.b, C1, IndexedFile("a.py", "ba01"))
        # An unpublished attempt's rows stay invisible.
        fence = await _claim(index, index.a)
        await _write(
            index,
            index.a,
            fence,
            IndexedFile("d.py", "ad02", symbols=(_symbol("d.py", "foo", 1, 1),)),
        )

        async with index.uow() as uow:
            store = uow.index
            assert await store.iter_files(index.a, None, 2) == [("a.py", "aa01"), ("b.py", "ab01")]
            assert await store.iter_files(index.a, "b.py", 2) == [("c.py", "ac01")]
            assert await store.iter_files(index.a, "c.py", 2) == []
            first = await store.iter_imports(index.a, None, 2)
            assert first == [
                ImportRecord("b.py", ".a", ImportResolution.RESOLVED, "a.py"),
                ImportRecord("b.py", "requests", ImportResolution.EXTERNAL, None),
            ]
            # The keyset includes the resolution: one specifier, two records.
            assert await store.iter_imports(index.a, first[-1], 1) == [resolved]
            assert await store.iter_imports(index.a, resolved, 1) == [dynamic]
            assert await store.iter_imports(index.a, dynamic, 1) == []
            assert await store.visible_counts(
                index.a, ["a.py", "b.py", "d.py", "e.py"]
            ) == IndexCounts(file_count=2, symbol_count=2, edge_count=2)
            assert await store.visible_counts(index.a, []) == IndexCounts(0, 0, 0)
            assert await store.iter_files(index.b, None, 10) == [("a.py", "ba01")]
            assert await store.iter_imports(index.b, None, 10) == []

        await _purge(index, index.a)
        async with index.uow() as uow:
            assert await uow.index.iter_files(index.a, None, 10) == []
            assert await uow.index.iter_imports(index.a, None, 10) == []
            assert await uow.index.visible_counts(index.a, ["a.py", "b.py"]) == IndexCounts(0, 0, 0)

    asyncio.run(scenario())
