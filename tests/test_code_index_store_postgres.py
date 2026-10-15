"""PostgreSQL evidence for the graph index schema (#141, ADR 0001 §4 and §6.2).

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from enum import StrEnum
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
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


def _repository(connection: Connection) -> str:
    workspace_id, installation_id, repository_id = str(uuid4()), str(uuid4()), str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', 17, '{\"account_login\": \"octo\"}')"
        ),
        {"id": installation_id, "workspace": workspace_id},
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
