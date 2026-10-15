"""PostgreSQL evidence for the graph index schema and store (#141, ADR 0001 §4 and §6.2).

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.index_store import (
    CodeIndexStore,
    EdgeTarget,
    ImportRecord,
    IndexBuildStats,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
    IndexFence,
    IndexLeaseLost,
    IndexProfile,
    IndexState,
)
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
from app.modules.code_index.domain.keys import (
    MAX_INDEX_KEY_BYTES,
    SymbolKey,
    assign_disambiguators,
)
from app.modules.code_index.infrastructure.index_store import (
    SqlAlchemyCodeIndexStore,
    SqlAlchemyCodeIndexUnitOfWork,
)
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import SymbolKind

_GRAPH_KEY = {
    "repository_id": ("uuid", False, None),
    "generation": ("int8", False, None),
    "valid_from": ("int8", False, None),
    "valid_to": ("int8", True, None),
    "path": ("varchar", False, 1024),
}

# (udt_name, nullable, character_maximum_length): no text, bytea, jsonb or vector column
# exists, so the index cannot hold code text, signatures or embeddings.
_COLUMNS = {
    "code_index_states": {
        "repository_id": ("uuid", False, None),
        "generation": ("int8", False, None),
        "reserved_publication": ("int8", False, None),
        "published_publication": ("int8", False, None),
        "floor_publication": ("int8", False, None),
        "status": ("code_index_status", False, None),
        "purge_reason": ("code_index_purge_reason", True, None),
        "error_code": ("varchar", True, 64),
        "last_error_code": ("varchar", True, 64),
        "indexed_commit_sha": ("varchar", True, 64),
        "target_commit_sha": ("varchar", True, 64),
        "published_at": ("timestamptz", True, None),
        "last_used_at": ("timestamptz", True, None),
        "lease_token": ("uuid", True, None),
        "lease_expires_at": ("timestamptz", True, None),
        "file_count": ("int4", False, None),
        "symbol_count": ("int4", False, None),
        "edge_count": ("int4", False, None),
        "extractor_version": ("varchar", True, 64),
        "resolver_version": ("varchar", True, 64),
        "parser_profile_version": ("varchar", True, 64),
        "limits_profile_version": ("varchar", True, 64),
        "downloaded_bytes": ("int8", False, None),
        "build_duration_ms": ("int4", False, None),
        "github_request_count": ("int4", False, None),
        "created_at": ("timestamptz", False, None),
        "updated_at": ("timestamptz", False, None),
    },
    "code_index_files": {**_GRAPH_KEY, "blob_sha": ("varchar", False, 64)},
    "code_index_symbols": {
        **_GRAPH_KEY,
        "qualified_name": ("varchar", False, 512),
        "kind": ("code_index_symbol_kind", False, None),
        "disambiguator": ("int4", False, None),
        "start_line": ("int4", False, None),
        "end_line": ("int4", False, None),
    },
    "code_index_edges": {
        **_GRAPH_KEY,
        "kind": ("code_index_edge_kind", False, None),
        "target_path": ("varchar", False, 1024),
        "target_name": ("varchar", False, 512),
    },
    "code_index_imports": {
        **_GRAPH_KEY,
        "specifier": ("varchar", False, 1024),
        "resolution": ("code_index_import_resolution", False, None),
        "target_path": ("varchar", True, 1024),
    },
}

_PRIMARY_KEYS = {
    "code_index_states": ["repository_id"],
    "code_index_files": ["repository_id", "generation", "valid_from", "path"],
    "code_index_symbols": [
        "repository_id",
        "generation",
        "valid_from",
        "path",
        "qualified_name",
        "kind",
        "disambiguator",
    ],
    "code_index_edges": [
        "repository_id",
        "generation",
        "valid_from",
        "path",
        "kind",
        "target_path",
        "target_name",
    ],
    "code_index_imports": [
        "repository_id",
        "generation",
        "valid_from",
        "path",
        "specifier",
        "resolution",
    ],
}

_CHECKS = {
    "code_index_states": {
        "ck_code_index_states_failed_error",
        "ck_code_index_states_lease_pair",
        "ck_code_index_states_purge_reason",
    },
    "code_index_files": set(),
    "code_index_symbols": {"ck_code_index_symbols_lines"},
    "code_index_edges": {"ck_code_index_edges_target_name"},
    "code_index_imports": set(),
}

_INDEXES = {
    "code_index_states": {},
    "code_index_files": {},
    "code_index_symbols": {},
    "code_index_edges": {
        "ix_code_index_edges_target": [
            "repository_id",
            "generation",
            "target_path",
            "target_name",
        ]
    },
    "code_index_imports": {
        "ix_code_index_imports_target": ["repository_id", "generation", "target_path"]
    },
}

_SYMBOL = (
    "INSERT INTO code_index_symbols (repository_id, generation, valid_from, path, "
    "qualified_name, kind, disambiguator, start_line, end_line) "
    "VALUES (:repository, 1, 1, 'a.py', 'foo', 'function', 0, :start, :end)"
)
_EDGE = (
    "INSERT INTO code_index_edges (repository_id, generation, valid_from, path, kind, "
    "target_path, target_name) VALUES (:repository, 1, 1, 'b.py', :kind, 'a.py', :name)"
)


@pytest.fixture
def code_index_database() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_code_index_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                command.upgrade(_migration_config(connection), "head")
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _migration_config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _repository(connection: Connection, installation_external_id: int = 17) -> str:
    workspace_id, installation_id, repository_id = str(uuid4()), str(uuid4()), str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', :external_id, '{\"account_login\": \"octo\"}')"
        ),
        {"id": installation_id, "workspace": workspace_id, "external_id": installation_external_id},
    )
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, 101, 'octo/repo', 'main', "
            "'https://github.com/octo/repo')"
        ),
        {"id": repository_id, "installation": installation_id},
    )
    return repository_id


def _index_tables(connection: Connection) -> set[str]:
    return set(
        connection.scalars(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                "AND starts_with(tablename, 'code_index_')"
            )
        )
    )


def _index_enum_types(connection: Connection) -> set[str]:
    return set(
        connection.scalars(
            text(
                "SELECT typname FROM pg_type WHERE typtype = 'e' "
                "AND typnamespace = current_schema()::regnamespace "
                "AND starts_with(typname, 'code_index_')"
            )
        )
    )


@pytest.mark.integration
def test_index_tables_have_exactly_the_planned_columns(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    columns = {
        table: {
            name: (udt_name, nullable == "YES", length)
            for name, udt_name, nullable, length in connection.execute(
                text(
                    "SELECT column_name, udt_name, is_nullable, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table"
                ),
                {"table": table},
            )
        }
        for table in _COLUMNS
    }
    assert columns == _COLUMNS


@pytest.mark.integration
def test_index_tables_have_ordered_natural_primary_keys(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    primary_keys = {
        table: list(
            connection.scalars(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                    "WHERE i.indrelid = CAST(:table AS regclass) AND i.indisprimary "
                    "ORDER BY k.ord"
                ),
                {"table": table},
            )
        )
        for table in _PRIMARY_KEYS
    }
    assert primary_keys == _PRIMARY_KEYS


@pytest.mark.integration
def test_every_index_table_cascades_from_its_repository(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    foreign_keys = {
        (table, referenced, delete_action, tuple(columns))
        for table, referenced, delete_action, columns in connection.execute(
            text(
                "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text, "
                "c.confdeltype::text, "
                "ARRAY(SELECT a.attname FROM unnest(c.conkey) AS k(attnum) "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) "
                "FROM pg_constraint c "
                "WHERE c.contype = 'f' AND c.connamespace = current_schema()::regnamespace "
                "AND starts_with(c.conrelid::regclass::text, 'code_index_')"
            )
        )
    }
    assert foreign_keys == {
        ("code_index_states", "repositories", "c", ("repository_id",)),
        ("code_index_files", "repositories", "c", ("repository_id",)),
        ("code_index_symbols", "repositories", "c", ("repository_id",)),
        ("code_index_edges", "repositories", "c", ("repository_id",)),
        ("code_index_imports", "repositories", "c", ("repository_id",)),
    }


@pytest.mark.integration
def test_index_tables_carry_the_named_checks_and_target_indexes(
    code_index_database: Connection,
) -> None:
    inspector = inspect(code_index_database)
    checks = {
        table: {check["name"] for check in inspector.get_check_constraints(table)}
        for table in _CHECKS
    }
    indexes = {
        table: {index["name"]: index["column_names"] for index in inspector.get_indexes(table)}
        for table in _INDEXES
    }
    assert checks == _CHECKS
    assert indexes == _INDEXES


@pytest.mark.integration
@pytest.mark.parametrize(
    ("type_name", "enum_class", "values"),
    [
        ("code_index_status", IndexStatus, ["empty", "building", "ready", "failed"]),
        ("code_index_purge_reason", PurgeReason, ["ttl", "removed", "disabled"]),
        ("code_index_edge_kind", EdgeKind, ["imports", "uses_imported"]),
        (
            "code_index_symbol_kind",
            SymbolKind,
            ["function", "method", "class", "variable", "type"],
        ),
        (
            "code_index_import_resolution",
            ImportResolution,
            ["resolved", "external", "ambiguous", "dynamic", "excluded", "unsupported", "missing"],
        ),
    ],
)
def test_enum_types_match_their_domain_enums(
    code_index_database: Connection,
    type_name: str,
    enum_class: type[StrEnum],
    values: list[str],
) -> None:
    labels = code_index_database.scalar(text(f"SELECT enum_range(NULL::{type_name})::text[]"))
    # The extractor and resolver enums of #130 are reused, so a new member without a
    # migration fails here instead of failing an index write.
    assert labels == values
    assert [member.value for member in enum_class] == values


@pytest.mark.integration
def test_state_row_defaults_to_an_empty_unpublished_index(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    repository_id = _repository(connection)
    connection.execute(
        text("INSERT INTO code_index_states (repository_id) VALUES (:id)"), {"id": repository_id}
    )
    row = connection.execute(
        text(
            "SELECT generation, reserved_publication, published_publication, "
            "floor_publication, status::text, purge_reason, error_code, last_error_code, "
            "indexed_commit_sha, target_commit_sha, published_at, last_used_at, lease_token, "
            "lease_expires_at, file_count, symbol_count, edge_count, extractor_version, "
            "resolver_version, parser_profile_version, limits_profile_version, "
            "downloaded_bytes, build_duration_ms, github_request_count, "
            "created_at IS NOT NULL, updated_at IS NOT NULL FROM code_index_states"
        )
    ).one()
    assert tuple(row) == (
        *(0, 0, 0, 0),
        "empty",
        *(None,) * 9,
        *(0, 0, 0),
        *(None,) * 4,
        *(0, 0, 0),
        True,
        True,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("statement", "params", "constraint"),
    [
        pytest.param(
            "INSERT INTO code_index_states (repository_id, status) VALUES (:repository, 'failed')",
            {},
            "ck_code_index_states_failed_error",
            id="failed-without-error-code",
        ),
        pytest.param(
            "INSERT INTO code_index_states (repository_id, error_code) "
            "VALUES (:repository, 'parse_failed')",
            {},
            "ck_code_index_states_failed_error",
            id="error-code-outside-failed",
        ),
        pytest.param(
            "INSERT INTO code_index_states (repository_id, lease_token) "
            "VALUES (:repository, gen_random_uuid())",
            {},
            "ck_code_index_states_lease_pair",
            id="lease-token-without-expiry",
        ),
        pytest.param(
            "INSERT INTO code_index_states (repository_id, lease_expires_at) "
            "VALUES (:repository, now())",
            {},
            "ck_code_index_states_lease_pair",
            id="lease-expiry-without-token",
        ),
        pytest.param(
            "INSERT INTO code_index_states (repository_id, status, purge_reason) "
            "VALUES (:repository, 'building', 'ttl')",
            {},
            "ck_code_index_states_purge_reason",
            id="purge-reason-outside-empty",
        ),
        pytest.param(
            _SYMBOL, {"start": 0, "end": 1}, "ck_code_index_symbols_lines", id="line-zero"
        ),
        pytest.param(
            _SYMBOL,
            {"start": 5, "end": 4},
            "ck_code_index_symbols_lines",
            id="end-before-start",
        ),
        pytest.param(
            _EDGE,
            {"kind": "imports", "name": "foo"},
            "ck_code_index_edges_target_name",
            id="imports-edge-with-name",
        ),
        pytest.param(
            _EDGE,
            {"kind": "uses_imported", "name": ""},
            "ck_code_index_edges_target_name",
            id="uses-imported-edge-without-name",
        ),
    ],
)
def test_check_constraints_reject_inconsistent_rows(
    code_index_database: Connection,
    statement: str,
    params: dict[str, object],
    constraint: str,
) -> None:
    connection = code_index_database
    repository_id = _repository(connection)
    with pytest.raises(IntegrityError, match=constraint), connection.begin_nested():
        connection.execute(text(statement), {"repository": repository_id, **params})


@pytest.mark.integration
def test_check_constraints_accept_consistent_rows(code_index_database: Connection) -> None:
    connection = code_index_database
    repository_id = _repository(connection)
    connection.execute(
        text(
            "INSERT INTO code_index_states "
            "(repository_id, status, error_code, lease_token, lease_expires_at) "
            "VALUES (:repository, 'failed', 'parse_failed', gen_random_uuid(), now())"
        ),
        {"repository": repository_id},
    )
    connection.execute(
        text(
            "UPDATE code_index_states SET status = 'empty', error_code = NULL, "
            "purge_reason = 'disabled', lease_token = NULL, lease_expires_at = NULL"
        )
    )
    connection.execute(
        text(
            "INSERT INTO code_index_files (repository_id, generation, valid_from, path, blob_sha) "
            "VALUES (:repository, 1, 1, 'a.py', 'abc')"
        ),
        {"repository": repository_id},
    )
    connection.execute(text(_SYMBOL), {"repository": repository_id, "start": 1, "end": 1})
    connection.execute(text(_EDGE), {"repository": repository_id, "kind": "imports", "name": ""})
    connection.execute(
        text(_EDGE), {"repository": repository_id, "kind": "uses_imported", "name": "foo"}
    )
    connection.execute(
        text(
            "INSERT INTO code_index_imports "
            "(repository_id, generation, valid_from, path, specifier, resolution) "
            "VALUES (:repository, 1, 1, 'b.py', 'requests', 'external')"
        ),
        {"repository": repository_id},
    )
    assert connection.execute(
        text(
            "SELECT (SELECT count(*) FROM code_index_files), "
            "(SELECT count(*) FROM code_index_symbols), (SELECT count(*) FROM code_index_edges), "
            "(SELECT count(*) FROM code_index_imports), "
            "(SELECT status::text || '/' || purge_reason::text FROM code_index_states)"
        )
    ).one() == (1, 1, 2, 1, "empty/disabled")


@pytest.mark.integration
def test_downgrade_drops_index_tables_and_types_and_upgrade_restores_them(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    config = _migration_config(connection)
    assert _index_tables(connection) == set(_COLUMNS)
    connection.rollback()
    command.downgrade(config, "20261014_0031")
    assert _index_tables(connection) == set()
    assert _index_enum_types(connection) == set()
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261014_0031"
    connection.rollback()
    command.upgrade(config, "head")
    assert _index_tables(connection) == set(_COLUMNS)
    assert _index_enum_types(connection) == {
        "code_index_status",
        "code_index_purge_reason",
        "code_index_symbol_kind",
        "code_index_edge_kind",
        "code_index_import_resolution",
    }
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261015_0032"


# Store behaviour: fencing, publication, purge, garbage collection and isolation.

C1, C2, C3 = "1" * 40, "2" * 40, "3" * 40
_PROFILE = IndexProfile("facts-3", "resolver-2", "parser-1", "limits-1")
_STATS = IndexBuildStats(downloaded_bytes=48_213, build_duration_ms=1_250, github_request_count=3)
_PROFILE_FIELDS = {
    "extractor_version": "facts-3",
    "resolver_version": "resolver-2",
    "parser_profile_version": "parser-1",
    "limits_profile_version": "limits-1",
}
_STATS_FIELDS = {"downloaded_bytes": 48_213, "build_duration_ms": 1_250, "github_request_count": 3}
_COUNT_ROWS = (
    "SELECT (SELECT count(*) FROM code_index_files WHERE repository_id = :repository), "
    "(SELECT count(*) FROM code_index_symbols WHERE repository_id = :repository), "
    "(SELECT count(*) FROM code_index_edges WHERE repository_id = :repository), "
    "(SELECT count(*) FROM code_index_imports WHERE repository_id = :repository)"
)
_FILE_ROWS = (
    "SELECT generation, valid_from, valid_to, path, blob_sha FROM code_index_files "
    "WHERE repository_id = :repository ORDER BY generation, valid_from, path"
)
# 2, 2 and 4 UTF-8 bytes per character.
_E, _A, _WIDE = "é", "ä", "\U0001d51e"


@dataclass(frozen=True)
class _Index:
    factory: async_sessionmaker[AsyncSession]
    a: UUID
    b: UUID

    def uow(self) -> SqlAlchemyCodeIndexUnitOfWork:
        return SqlAlchemyCodeIndexUnitOfWork(self.factory)


@pytest.fixture
def index(code_index_database: Connection) -> Iterator[_Index]:
    """Two workspace/installation/repository sets, committed so async sessions see them."""
    connection = code_index_database
    repository_a = UUID(_repository(connection, installation_external_id=17))
    repository_b = UUID(_repository(connection, installation_external_id=18))
    connection.commit()
    schema = connection.scalar(text("SELECT current_schema()"))
    engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    try:
        yield _Index(async_sessionmaker(engine, expire_on_commit=False), repository_a, repository_b)
    finally:
        asyncio.run(engine.dispose())


def _symbol(
    path: str, name: str, start: int, end: int, kind: SymbolKind = SymbolKind.FUNCTION
) -> IndexedSymbol:
    return IndexedSymbol(SymbolKey(path, name, kind, 0), start, end)


def _uses(path: str, target_path: str, name: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.USES_IMPORTED, target_path, name)


def _imports(path: str, target_path: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.IMPORTS, target_path, "")


def _module(blob: str, start: int) -> IndexedFile:
    """`a.py` defining `foo` at line `start`."""
    return IndexedFile("a.py", blob, symbols=(_symbol("a.py", "foo", start, start + 1),))


def _importer(blob: str, start: int) -> IndexedFile:
    """`b.py` declaring class `Bar`; it imports `a.py`, uses its `foo` and imports `requests`."""
    return IndexedFile(
        "b.py",
        blob,
        symbols=(_symbol("b.py", "Bar", start, start + 4, SymbolKind.CLASS),),
        edges=(_imports("b.py", "a.py"), _uses("b.py", "a.py", "foo")),
        imports=(
            ImportRecord("b.py", ".a", ImportResolution.RESOLVED, "a.py"),
            ImportRecord("b.py", "requests", ImportResolution.EXTERNAL, None),
        ),
    )


async def _enable(index: _Index, repository_id: UUID) -> IndexState:
    async with index.uow() as uow:
        state = await uow.index.enable(repository_id)
        await uow.commit()
    return state


async def _purge(
    index: _Index, repository_id: UUID, reason: PurgeReason = PurgeReason.DISABLED
) -> None:
    async with index.uow() as uow:
        await uow.index.purge(repository_id, reason)
        await uow.commit()


async def _try_claim(index: _Index, repository_id: UUID, generation: int) -> IndexFence | None:
    async with index.uow() as uow:
        fence = await uow.index.claim_lease(repository_id, generation, 600)
        await uow.commit()
    return fence


async def _claim(index: _Index, repository_id: UUID, generation: int = 1) -> IndexFence:
    fence = await _try_claim(index, repository_id, generation)
    assert fence is not None
    return fence


async def _write(
    index: _Index, repository_id: UUID, fence: IndexFence, *files: IndexedFile
) -> None:
    for file in files:
        async with index.uow() as uow:
            await uow.index.replace_file(repository_id, fence, file)
            await uow.commit()


async def _publish(
    index: _Index,
    repository_id: UUID,
    fence: IndexFence,
    commit: str,
    *,
    base: str | None = None,
    full: bool = True,
    deleted: Sequence[str] = (),
) -> bool:
    async with index.uow() as uow:
        published = await uow.index.publish(
            repository_id,
            fence,
            base_commit_sha=base,
            commit_sha=commit,
            full=full,
            deleted_paths=deleted,
            profile=_PROFILE,
            stats=_STATS,
        )
        await uow.commit()
    return published


async def _build(
    index: _Index,
    repository_id: UUID,
    commit: str,
    *files: IndexedFile,
    base: str | None = None,
    full: bool = True,
    deleted: Sequence[str] = (),
) -> None:
    fence = await _claim(index, repository_id)
    await _write(index, repository_id, fence, *files)
    assert await _publish(
        index, repository_id, fence, commit, base=base, full=full, deleted=deleted
    )


async def _read[T](index: _Index, read: Callable[[CodeIndexStore], Awaitable[T]]) -> T:
    async with index.uow() as uow:
        return await read(uow.index)


async def _state(index: _Index, repository_id: UUID) -> IndexState | None:
    return await _read(index, lambda store: store.get_state(repository_id))


async def _reads(index: _Index, repository_id: UUID) -> tuple[object, ...]:
    """Every read method of the store, on the paths and names both repositories share."""
    async with index.uow() as uow:
        store = uow.index
        return (
            await store.get_file(repository_id, "a.py"),
            await store.file_symbols(repository_id, "b.py"),
            await store.outgoing_edges(repository_id, "b.py"),
            await store.incoming_edges(repository_id, "a.py", "foo"),
            await store.importers_of(repository_id, "a.py"),
        )


async def _collect(index: _Index, repository_id: UUID, limit: int) -> list[int]:
    """Run garbage collection in portions until one deletes nothing."""
    collected: list[int] = []
    while not collected or collected[-1]:
        async with index.uow() as uow:
            collected.append(await uow.index.collect_garbage(repository_id, limit))
            await uow.commit()
    return collected


async def _rows(index: _Index, statement: str, **values: object) -> list[tuple[Any, ...]]:
    async with index.factory() as session:
        return [tuple(row) for row in await session.execute(text(statement), values)]


async def _execute(index: _Index, statement: str, repository_id: UUID) -> None:
    async with index.factory.begin() as session:
        await session.execute(text(statement), {"repository": repository_id})


async def _expire_lease(index: _Index, repository_id: UUID) -> None:
    await _execute(
        index,
        "UPDATE code_index_states SET lease_expires_at = clock_timestamp() - interval '1 second' "
        "WHERE repository_id = :repository",
        repository_id,
    )


async def _wait_until_blocked(
    index: _Index, blocker_pid: int, waiters: int, *tasks: asyncio.Task[Any]
) -> None:
    """Wait until `waiters` sessions wait on `blocker_pid`, directly or behind another waiter.

    A second waiter on one row queues behind the first for the tuple lock, so the chain
    is followed rather than only the direct blocker.
    """
    async with asyncio.timeout(5), index.factory() as observer:
        while (
            await observer.scalar(
                text(
                    "WITH RECURSIVE blocked(pid) AS ("
                    "SELECT pid FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)) "
                    "UNION SELECT a.pid FROM pg_stat_activity a "
                    "JOIN blocked b ON b.pid = ANY(pg_blocking_pids(a.pid))) "
                    "SELECT count(*) FROM blocked"
                ),
                {"pid": blocker_pid},
            )
            < waiters
        ):
            assert not any(task.done() for task in tasks), "a session did not wait for the lock"
            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            await asyncio.sleep(0.01)
    assert not any(task.done() for task in tasks)


def _pause_after_lock(
    monkeypatch: pytest.MonkeyPatch, store: SqlAlchemyCodeIndexStore
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold `store` at its clock read, which every fenced step makes after its row lock."""
    locked, release = asyncio.Event(), asyncio.Event()
    clock = store._clock

    async def paused() -> datetime:
        locked.set()
        await release.wait()
        return await clock()

    monkeypatch.setattr(store, "_clock", paused)
    return locked, release


def _fields(state: IndexState | None) -> dict[str, object]:
    """The state without its clock values, which tests compare to the database clock."""
    assert state is not None
    fields = dataclasses.asdict(state)
    for name in ("published_at", "last_used_at", "lease_expires_at", "updated_at"):
        del fields[name]
    return fields


@pytest.mark.integration
def test_store_flushes_and_only_the_callers_commit_persists(index: _Index) -> None:
    async def scenario() -> None:
        async with index.uow() as uow:
            await uow.index.enable(index.a)
            fence = await uow.index.claim_lease(index.a, 1, 600)
            assert fence is not None
            await uow.index.replace_file(index.a, fence, _module("aa01", 1))
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
            "indexed_commit_sha": C1,
            "target_commit_sha": None,
            "reserved_publication": 1,
            "published_publication": 1,
            "floor_publication": 1,
            "file_count": 2,
            "symbol_count": 2,
            "edge_count": 2,
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
            "indexed_commit_sha": C2,
            "target_commit_sha": None,
            "reserved_publication": 2,
            "published_publication": 2,
            "floor_publication": 1,
            "file_count": 2,
            "symbol_count": 3,
            "edge_count": 2,
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
            "indexed_commit_sha": None,
            "target_commit_sha": None,
            "reserved_publication": 1,
            "published_publication": 0,
            "floor_publication": 0,
            "file_count": 0,
            "symbol_count": 0,
            "edge_count": 0,
            "profile": None,
            "stats": {"downloaded_bytes": 0, "build_duration_ms": 0, "github_request_count": 0},
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
            "indexed_commit_sha": None,
            "target_commit_sha": None,
            "reserved_publication": 0,
            "published_publication": 0,
            "floor_publication": 0,
            "file_count": 0,
            "symbol_count": 0,
            "edge_count": 0,
            "profile": None,
            "stats": {"downloaded_bytes": 0, "build_duration_ms": 0, "github_request_count": 0},
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
        assert await _try_claim(index, index.a, 1) is None

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
def test_claim_takes_a_free_or_expired_lease_of_the_current_generation_only(
    index: _Index,
) -> None:
    async def scenario() -> None:
        assert await _try_claim(index, index.a, 1) is None
        await _enable(index, index.a)
        assert await _try_claim(index, index.a, 2) is None
        first = await _claim(index, index.a)
        assert (first.generation, first.publication) == (1, 1)
        assert await _try_claim(index, index.a, 1) is None
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
            "lease_token = NULL, lease_expires_at = NULL WHERE repository_id = :repository",
            index.a,
        )
        third = await _claim(index, index.a)
        assert third.publication == 3
        claimed = await _state(index, index.a)
        assert claimed is not None and (claimed.status, claimed.error_code) == ("building", None)

        await _purge(index, index.a)
        # Only enable re-arms a purged repository.
        assert await _try_claim(index, index.a, 2) is None
        purged = await _state(index, index.a)
        assert purged is not None
        assert (purged.generation, purged.status, purged.reserved_publication) == (2, "empty", 3)

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
        assert await revoke() is True
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
            claiming = asyncio.create_task(store.claim_lease(index.a, 1, 600))
            await asyncio.wait_for(locked.wait(), 5)
            duplicate = asyncio.create_task(_try_claim(index, index.a, 1))
            await _wait_until_blocked(index, first_pid, 1, duplicate)
            release.set()
            fence = await claiming
            await first.commit()

        assert await asyncio.wait_for(duplicate, 5) is None
        assert fence is not None and (fence.generation, fence.publication) == (1, 1)
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
            fence = await uow.index.claim_lease(index.a, 1, 1)
            await uow.commit()
        assert fence is not None
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
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1))
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _module("aa02", 1))
        # The build started from C2, but the published index is at C1.
        assert not await _publish(index, index.a, fence, C3, base=C2, full=False)

        missed = await _state(index, index.a)
        assert _fields(missed) == {
            "generation": 1,
            "status": "ready",
            "purge_reason": None,
            "error_code": None,
            "last_error_code": None,
            "indexed_commit_sha": C1,
            "target_commit_sha": None,
            "reserved_publication": 2,
            "published_publication": 1,
            "floor_publication": 1,
            "file_count": 1,
            "symbol_count": 1,
            "edge_count": 0,
            "profile": _PROFILE_FIELDS,
            "stats": _STATS_FIELDS,
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
