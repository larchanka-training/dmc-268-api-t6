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
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast
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
    ClaimRefusal,
    ClaimRefused,
    CodeIndexStore,
    EdgeTarget,
    ImportRecord,
    IndexAdmission,
    IndexBuildStats,
    IndexCounts,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
    IndexFence,
    IndexLeaseLost,
    IndexProfile,
    IndexState,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.domain.codes import FAILED_ERROR_CODES
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
from app.modules.code_index.domain.keys import (
    MAX_INDEX_KEY_BYTES,
    SymbolKey,
    assign_disambiguators,
)
from app.modules.code_index.domain.storage import COLUMN_WEIGHTS, ROW_OVERHEAD_BYTES, row_bytes
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
        "indexed_default_branch": ("varchar", True, 255),
        "exclusions_digest": ("varchar", True, 64),
        "stored_bytes": ("int8", False, None),
        "pending_bytes": ("int8", False, None),
        "wake_requested_at": ("timestamptz", True, None),
        "review_full_requested_at": ("timestamptz", True, None),
        "review_full_requested_sha": ("varchar", True, 64),
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

# 0032 (#141) creates these; 0033 (#145) does not touch `code_index_states`.
_CHECKS_0033 = {
    "ck_code_index_states_failed_error",
    "ck_code_index_states_lease_pair",
    "ck_code_index_states_purge_reason",
}
_CHECKS = {
    "code_index_states": {
        *_CHECKS_0033,
        "ck_code_index_states_profile_complete",
        "ck_code_index_states_bytes",
        "ck_code_index_states_pending_leased",
        "ck_code_index_states_failed_code",
        "ck_code_index_states_wake_live",
        "ck_code_index_states_review_full_sha_at",
    },
    "code_index_files": set(),
    "code_index_symbols": {"ck_code_index_symbols_lines"},
    "code_index_edges": {"ck_code_index_edges_target_name"},
    "code_index_imports": set(),
}

_INDEXES = {
    "code_index_states": {
        "ix_code_index_states_live_lease": ["repository_id"],
        "ix_code_index_states_wake_requested": ["wake_requested_at"],
    },
    "code_index_files": {"ix_code_index_files_closed": ["repository_id"]},
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


def _sibling(
    connection: Connection,
    repository_id: UUID,
    *,
    external_id: int,
    installation_external_id: int | None = None,
) -> UUID:
    """Another repository in `repository_id`'s installation, or, given an installation
    external id, in a new installation of its Workspace; committed for async sessions."""
    installation = connection.scalar(
        text("SELECT provider_installation_id FROM repositories WHERE id = :id"),
        {"id": repository_id},
    )
    if installation_external_id is not None:
        installation = connection.scalar(
            text(
                "INSERT INTO provider_installations "
                "(id, workspace_id, provider, external_id, metadata) "
                "SELECT gen_random_uuid(), workspace_id, 'github', :external_id, "
                '\'{"account_login": "octo"}\' FROM provider_installations WHERE id = :id '
                "RETURNING id"
            ),
            {"id": installation, "external_id": installation_external_id},
        )
    sibling = uuid4()
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, :external_id, 'octo/sibling', 'main', "
            "'https://github.com/octo/sibling')"
        ),
        {"id": sibling, "installation": installation, "external_id": external_id},
    )
    connection.commit()
    return sibling


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
            "created_at IS NOT NULL, updated_at IS NOT NULL, indexed_default_branch, "
            "exclusions_digest, stored_bytes, pending_bytes FROM code_index_states"
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
        *(None, None),
        *(0, 0),
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
            "INSERT INTO code_index_states (repository_id, extractor_version, resolver_version, "
            "parser_profile_version, limits_profile_version) "
            "VALUES (:repository, 'facts-3', 'resolver-2', 'parser-1', 'limits-1')",
            {},
            "ck_code_index_states_profile_complete",
            id="profile-without-branch-and-digest",
        ),
        pytest.param(
            "INSERT INTO code_index_states (repository_id, stored_bytes) VALUES (:repository, -1)",
            {},
            "ck_code_index_states_bytes",
            id="negative-stored-bytes",
        ),
        pytest.param(
            "INSERT INTO code_index_states "
            "(repository_id, pending_bytes, lease_token, lease_expires_at) "
            "VALUES (:repository, -1, gen_random_uuid(), now())",
            {},
            "ck_code_index_states_bytes",
            id="negative-pending-bytes",
        ),
        pytest.param(
            "INSERT INTO code_index_states (repository_id, pending_bytes) VALUES (:repository, 1)",
            {},
            "ck_code_index_states_pending_leased",
            id="pending-bytes-without-lease",
        ),
        pytest.param(
            "INSERT INTO code_index_states (repository_id, status, error_code) "
            "VALUES (:repository, 'failed', 'parse_failed')",
            {},
            "ck_code_index_states_failed_code",
            id="failed-with-another-code",
        ),
        pytest.param(
            "INSERT INTO code_index_states "
            "(repository_id, purge_reason, wake_requested_at) "
            "VALUES (:repository, 'disabled', now())",
            {},
            "ck_code_index_states_wake_live",
            id="wake-marker-on-a-purged-index",
        ),
        pytest.param(
            "INSERT INTO code_index_states "
            "(repository_id, status, error_code, wake_requested_at) "
            "VALUES (:repository, 'failed', 'archive_unsafe', now())",
            {},
            "ck_code_index_states_wake_live",
            id="wake-marker-on-a-failed-index",
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
            "(repository_id, status, error_code, lease_token, lease_expires_at, pending_bytes, "
            "stored_bytes, extractor_version, resolver_version, parser_profile_version, "
            "limits_profile_version, indexed_default_branch, exclusions_digest) "
            "VALUES (:repository, 'failed', 'limit_exceeded:edges', gen_random_uuid(), now(), "
            "7, 9, 'facts-3', 'resolver-2', 'parser-1', 'limits-1', 'main', 'digest')"
        ),
        {"repository": repository_id},
    )
    connection.execute(
        text(
            "UPDATE code_index_states SET status = 'empty', error_code = NULL, "
            "purge_reason = 'disabled', lease_token = NULL, lease_expires_at = NULL, "
            "pending_bytes = 0"
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
@pytest.mark.parametrize("status", ["empty", "building", "ready"])
def test_a_live_index_accepts_a_wake_marker(code_index_database: Connection, status: str) -> None:
    connection = code_index_database
    repository_id = _repository(connection)
    lease = "gen_random_uuid(), now()" if status == "building" else "NULL, NULL"
    connection.execute(
        text(
            "INSERT INTO code_index_states "
            "(repository_id, status, lease_token, lease_expires_at, wake_requested_at) "
            f"VALUES (:repository, '{status}', {lease}, now())"
        ),
        {"repository": repository_id},
    )
    assert connection.scalar(text("SELECT wake_requested_at IS NOT NULL FROM code_index_states"))


@pytest.mark.integration
def test_migration_0037_adds_the_wake_marker_and_its_downgrade_drops_it(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    config = _migration_config(connection)
    marker = (
        "SELECT (SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'code_index_states' "
        "AND column_name = 'wake_requested_at'), "
        "(SELECT count(*) FROM pg_constraint WHERE conname = 'ck_code_index_states_wake_live' "
        "AND connamespace = current_schema()::regnamespace), "
        "(SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname IN ('ix_code_index_states_wake_requested', 'ix_code_index_files_closed'))"
    )
    assert tuple(connection.execute(text(marker)).one()) == (1, 1, 2)
    assert [
        tuple(row)
        for row in connection.execute(
            text(
                "SELECT indexrelid::regclass::text, pg_get_expr(indpred, indrelid) FROM pg_index "
                "WHERE indrelid = 'code_index_files'::regclass AND NOT indisprimary"
            )
        )
    ] == [("ix_code_index_files_closed", "(valid_to IS NOT NULL)")]
    repository_id = _repository(connection)
    connection.execute(
        text(
            "INSERT INTO code_index_states (repository_id, generation, wake_requested_at) "
            "VALUES (:repository, 1, now())"
        ),
        {"repository": repository_id},
    )
    connection.commit()

    command.downgrade(config, "20261019_0036")
    assert tuple(connection.execute(text(marker)).one()) == (0, 0, 0)
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261019_0036"
    # The state row stays; only its pending wake-up request is lost.
    assert connection.scalar(text("SELECT generation FROM code_index_states")) == 1
    connection.commit()

    command.upgrade(config, "head")
    assert tuple(connection.execute(text(marker)).one()) == (1, 1, 2)
    assert connection.execute(
        text("SELECT generation, wake_requested_at FROM code_index_states")
    ).one() == (1, None)
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261027_0049"


@pytest.mark.integration
def test_migration_0038_adds_the_review_throttle_and_its_downgrade_drops_it(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    config = _migration_config(connection)
    throttle = (
        "SELECT (SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'code_index_states' "
        "AND column_name IN ('review_full_requested_at', 'review_full_requested_sha')), "
        "(SELECT count(*) FROM pg_constraint "
        "WHERE conname = 'ck_code_index_states_review_full_sha_at' "
        "AND connamespace = current_schema()::regnamespace)"
    )
    assert tuple(connection.execute(text(throttle)).one()) == (2, 1)
    repository_id = _repository(connection)
    # A sha implies a time; a NULL target records only the time.
    with (
        pytest.raises(IntegrityError, match="ck_code_index_states_review_full_sha_at"),
        connection.begin_nested(),
    ):
        connection.execute(
            text(
                "INSERT INTO code_index_states (repository_id, review_full_requested_sha) "
                "VALUES (:repository, :sha)"
            ),
            {"repository": repository_id, "sha": C1},
        )
    connection.execute(
        text(
            "INSERT INTO code_index_states (repository_id, generation, review_full_requested_at) "
            "VALUES (:repository, 1, now())"
        ),
        {"repository": repository_id},
    )
    connection.commit()

    command.downgrade(config, "20261020_0037")
    assert tuple(connection.execute(text(throttle)).one()) == (0, 0)
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261020_0037"
    # The state row stays; only the throttle of review-requested full builds is lost.
    assert connection.scalar(text("SELECT generation FROM code_index_states")) == 1
    connection.commit()

    command.upgrade(config, "head")
    assert tuple(connection.execute(text(throttle)).one()) == (2, 1)
    assert connection.execute(
        text(
            "SELECT generation, review_full_requested_at, review_full_requested_sha "
            "FROM code_index_states"
        )
    ).one() == (1, None, None)
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261027_0049"


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
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261027_0049"


# ADR 0001 §7: only a limit or an unsafe archive fails an index.
_FAILED_CODES = (
    "archive_unsafe",
    "limit_exceeded:download_bytes",
    "limit_exceeded:unpacked_bytes",
    "limit_exceeded:archive_entries",
    "limit_exceeded:parsed_files",
    "limit_exceeded:symbols",
    "limit_exceeded:edges",
    "limit_exceeded:deadline",
    "limit_exceeded:repository_storage",
    "limit_exceeded:workspace_storage",
)


@pytest.mark.integration
def test_failed_code_check_admits_every_failed_error_code(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    repository_id = _repository(connection)
    connection.execute(
        text("INSERT INTO code_index_states (repository_id) VALUES (:id)"), {"id": repository_id}
    )
    for code in _FAILED_CODES:
        connection.execute(
            text("UPDATE code_index_states SET status = 'failed', error_code = :code"),
            {"code": code},
        )
        assert connection.scalar(text("SELECT error_code FROM code_index_states")) == code
    assert frozenset(_FAILED_CODES) == FAILED_ERROR_CODES


# The partial indexes of `code_index_states` at head, with their predicates.
_STATE_INDEXES = {
    "ix_code_index_states_live_lease": "(lease_token IS NOT NULL)",
    "ix_code_index_states_wake_requested": "(wake_requested_at IS NOT NULL)",
}


def _state_schema(connection: Connection) -> tuple[set[str], set[str], dict[str, str]]:
    """The columns, CHECK names and index predicates of `code_index_states`."""
    columns = set(
        connection.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'code_index_states'"
            )
        )
    )
    checks = set(
        connection.scalars(
            text(
                "SELECT conname FROM pg_constraint WHERE contype = 'c' "
                "AND conrelid = 'code_index_states'::regclass"
            )
        )
    )
    indexes = {
        name: predicate
        for name, predicate in connection.execute(
            text(
                "SELECT indexrelid::regclass::text, pg_get_expr(indpred, indrelid) FROM pg_index "
                "WHERE indrelid = 'code_index_states'::regclass AND NOT indisprimary"
            )
        )
    }
    return columns, checks, indexes


@pytest.mark.integration
def test_downgrade_to_0033_drops_the_indexer_columns_and_upgrade_resets_profiles(
    code_index_database: Connection,
) -> None:
    connection = code_index_database
    config = _migration_config(connection)
    head_columns = set(_COLUMNS["code_index_states"])
    # 0034 adds the indexer columns; 0037 (#143) adds the wake-up marker; 0038 (#144) adds
    # the review full-build throttle.
    new_columns = {
        "indexed_default_branch",
        "exclusions_digest",
        "stored_bytes",
        "pending_bytes",
        "wake_requested_at",
        "review_full_requested_at",
        "review_full_requested_sha",
    }
    assert _state_schema(connection) == (head_columns, _CHECKS["code_index_states"], _STATE_INDEXES)
    connection.rollback()
    command.downgrade(config, "20261016_0033")
    assert _state_schema(connection) == (head_columns - new_columns, _CHECKS_0033, {})
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261016_0033"
    # The downgrade stops at #145's revision and keeps its RAG toggle column.
    assert (
        connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'repositories' "
                "AND column_name = 'rag_enabled'"
            )
        )
        == 1
    )
    repository_id = _repository(connection)
    connection.execute(
        text(
            "INSERT INTO code_index_states (repository_id, status, indexed_commit_sha, "
            "extractor_version, resolver_version, parser_profile_version, "
            "limits_profile_version) "
            "VALUES (:id, 'ready', :sha, 'facts-3', 'resolver-2', 'parser-1', 'limits-1')"
        ),
        {"id": repository_id, "sha": C1},
    )
    connection.commit()

    command.upgrade(config, "head")
    # The reset forces a full build: no publication on 0033 recorded a branch or digest.
    assert connection.execute(
        text(
            "SELECT status::text, indexed_commit_sha, extractor_version, resolver_version, "
            "parser_profile_version, limits_profile_version, indexed_default_branch, "
            "exclusions_digest, stored_bytes, pending_bytes FROM code_index_states"
        )
    ).one() == ("ready", C1, *(None,) * 6, 0, 0)
    assert _state_schema(connection) == (head_columns, _CHECKS["code_index_states"], _STATE_INDEXES)
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261027_0049"


# Store behaviour: fencing, publication, purge, garbage collection and isolation.

C1, C2, C3 = "1" * 40, "2" * 40, "3" * 40
_DIGEST = "d" * 64
_PROFILE = IndexProfile("facts-3", "resolver-2", "parser-1", "limits-1", "main", _DIGEST)
_STATS = IndexBuildStats(downloaded_bytes=48_213, build_duration_ms=1_250, github_request_count=3)
_PROFILE_FIELDS = {
    "extractor_version": "facts-3",
    "resolver_version": "resolver-2",
    "parser_profile_version": "parser-1",
    "limits_profile_version": "limits-1",
    "default_branch": "main",
    "exclusions_digest": _DIGEST,
}
_NO_STATS = {"downloaded_bytes": 0, "build_duration_ms": 0, "github_request_count": 0}
# ADR 0001 §7: 2 jobs in all, 1 per installation, 512 MiB per repository, 2 GiB per Workspace.
_ADMISSION = IndexAdmission(
    max_leases=2,
    max_installation_leases=1,
    repository_storage_bytes=512 * 1024 * 1024,
    workspace_storage_bytes=2 * 1024 * 1024 * 1024,
)
_STALE = ClaimRefused(ClaimRefusal.STALE)
_LEASED = ClaimRefused(ClaimRefusal.LEASED)
_BUSY = ClaimRefused(ClaimRefusal.BUSY)
# Estimated bytes (160 per row + w(c) · UTF-8 bytes) of the rows of `_module` and
# `_importer`:
# - `_module`: file a.py/aa01 160+2·4+4 = 172, symbol foo 160+2·4+2·3 = 174;
# - `_importer`: file 172, symbol Bar 174, imports edge 160+2·4+3·4 = 180, uses edge
#   180+3·3 = 189, import `.a` 160+2·4+2·2+2·4 = 180, import `requests` 160+2·4+2·8 = 184.
_MODULE_BYTES = 346
_IMPORTER_BYTES = 1_079
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


async def _try_claim(
    index: _Index, repository_id: UUID, generation: int, admission: IndexAdmission = _ADMISSION
) -> LeaseClaim | ClaimRefused:
    async with index.uow() as uow:
        claim = await uow.index.claim_lease(repository_id, generation, 600, admission)
        # Committed on a refusal too: a QUOTA refusal writes `failed`.
        await uow.commit()
    return claim


async def _claim(
    index: _Index, repository_id: UUID, generation: int = 1, admission: IndexAdmission = _ADMISSION
) -> IndexFence:
    claim = await _try_claim(index, repository_id, generation, admission)
    assert isinstance(claim, LeaseClaim)
    return claim.fence


async def _release(
    index: _Index,
    repository_id: UUID,
    fence: IndexFence,
    last_error_code: str | None = None,
    stats: IndexBuildStats | None = None,
) -> IndexStatus:
    async with index.uow() as uow:
        status = await uow.index.release(
            repository_id, fence, last_error_code=last_error_code, stats=stats
        )
        await uow.commit()
    return status


async def _write(
    index: _Index, repository_id: UUID, fence: IndexFence, *files: IndexedFile
) -> list[IndexStorage]:
    """One committed batch per file; the storage each batch returned."""
    storages: list[IndexStorage] = []
    for file in files:
        async with index.uow() as uow:
            storages.append(await uow.index.replace_file(repository_id, fence, file))
            await uow.commit()
    return storages


async def _publish(
    index: _Index,
    repository_id: UUID,
    fence: IndexFence,
    commit: str,
    *,
    base: str | None = None,
    full: bool = True,
    deleted: Sequence[str] = (),
    stats: IndexBuildStats = _STATS,
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
            stats=stats,
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


# The TTL purge (#143, ADR §4 lifetime row): any live index except `failed`.

_TTL = timedelta(days=30)


async def _purge_unused(index: _Index, repository_id: UUID) -> bool:
    async with index.uow() as uow:
        purged = await uow.index.purge_unused(repository_id, _TTL)
        await uow.commit()
    return purged


async def _unused_for_31_days(index: _Index, repository_id: UUID) -> None:
    await _execute(
        index,
        "UPDATE code_index_states SET last_used_at = clock_timestamp() - interval '31 days' "
        "WHERE repository_id = :repository",
        repository_id,
    )


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


# The indexer's store (#142): admission, failure, release, byte accounting and reads.


async def _mark_failed(
    index: _Index,
    repository_id: UUID,
    fence: IndexFence,
    error_code: str,
    stats: IndexBuildStats | None = None,
) -> None:
    async with index.uow() as uow:
        await uow.index.mark_failed(repository_id, fence, error_code, stats=stats)
        await uow.commit()


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


# The wake-up marker (#143): triggers request a build, the leader clears it with a CAS.


async def _marker(index: _Index, repository_id: UUID) -> datetime | None:
    [(marker,)] = await _rows(
        index,
        "SELECT wake_requested_at FROM code_index_states WHERE repository_id = :repository",
        repository=repository_id,
    )
    return cast(datetime | None, marker)


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
