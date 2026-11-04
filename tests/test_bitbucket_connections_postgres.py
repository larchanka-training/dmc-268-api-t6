"""Move-only baseline Bitbucket connection tests."""

from __future__ import annotations

from tests.bitbucket_connection_postgres_support import (
    Connection,
    MigrationContext,
    _config,
    command,
    compare_metadata,
    metadata,
    pytest,
    text,
)

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
def test_bitbucket_connection_migration_matches_metadata(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    assert "bitbucket_connections" in {
        row[0]
        for row in db.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")
        )
    }
    assert (
        compare_metadata(
            MigrationContext.configure(
                db, opts={"compare_type": True, "compare_server_default": True}
            ),
            metadata,
        )
        == []
    )
