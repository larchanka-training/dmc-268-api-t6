"""The dashboard read indexes of #456 Part 2: the migration creates them, its downgrade drops them.

The predicates are pinned as PostgreSQL prints them: a partial index serves only queries whose
WHERE implies it, so a predicate that drifts from the SQL the reads send is never used.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from app.modules.reviews.infrastructure.saved_review_results import load_review_results
from tests.portal_postgres import HEAD, RUN_ATTEMPTED, Env

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration

_REVISION = "20261109_0064"

_INDEXES = {
    "ix_findings_published_run_id": (
        "findings",
        "(run_id) INCLUDE (severity, category) "
        "WHERE ((published IS TRUE) AND (drop_reason IS NULL))",
    ),
    "ix_usage_events_run_totals": (
        "usage_events",
        "(run_id) INCLUDE (tokens_in, tokens_out, cost_usd)",
    ),
    "ix_run_actions_postprocess_run_index": (
        "run_actions",
        "(run_id, index DESC) WHERE ((tool)::text = ANY "
        "((ARRAY['review.finalize'::character varying, "
        "'review.postprocess'::character varying])::text[]))",
    ),
    "ix_context_payloads_summary_only_stop": (
        "context_payloads",
        "(run_id) WHERE (((summary ->> 'status'::text) = 'summary_only'::text) "
        "AND (NOT (summary ? 'reviewable_lines'::text)))",
    ),
}


def test_the_migration_adds_the_dashboard_indexes_and_its_downgrade_drops_them(
    portal_env: Env,
) -> None:
    engine = create_engine(portal_env.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'SET search_path TO "{portal_env.schema}"'))
            connection.commit()
            assert _definitions(connection) == {
                name: f"ON {table} USING btree {columns}"
                for name, (table, columns) in _INDEXES.items()
            }

            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.downgrade(config, "20261109_0066")
            connection.commit()
            assert _definitions(connection)["ix_run_actions_postprocess_run_index"] == (
                "ON run_actions USING btree (run_id, index DESC) "
                "WHERE ((tool)::text = 'review.postprocess'::text)"
            )

            down_revision = ScriptDirectory.from_config(config).get_revision(_REVISION)
            assert isinstance(down_revision.down_revision, str)
            command.downgrade(config, down_revision.down_revision)
            connection.commit()
            assert _definitions(connection) == {}

            command.upgrade(config, "head")
            connection.commit()
            assert set(_definitions(connection)) == set(_INDEXES)
    finally:
        engine.dispose()


def _definitions(connection: Connection) -> dict[str, str]:
    rows = connection.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = ANY(:names)"
        ),
        {"names": list(_INDEXES)},
    ).all()
    # indexdef is "CREATE INDEX <name> ON <schema>.<table> USING ..."; the schema is per test.
    return {
        name: "ON " + definition.split(" ON ", 1)[1].split(".", 1)[1] for name, definition in rows
    }


def test_saved_review_result_query_can_use_the_partial_index(portal_env: Env) -> None:
    captured: list[tuple[str, Any]] = []

    def capture(
        _connection: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any
    ) -> None:
        if statement.startswith("SELECT DISTINCT ON (run_actions.run_id)"):
            captured.append((statement, parameters))

    async def exercise() -> None:
        engine = portal_env.engine()
        try:
            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                async with async_sessionmaker(engine)() as session:
                    await load_review_results(session, {RUN_ATTEMPTED: HEAD}, {})
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert len(captured) == 1
            statement, parameters = captured[0]
            async with engine.connect() as connection:
                await connection.exec_driver_sql("SET enable_seqscan = off")
                explained = await connection.exec_driver_sql(
                    "EXPLAIN (FORMAT JSON) " + statement, parameters
                )
                nodes = [explained.scalar_one()[0]["Plan"]]
            for node in nodes:
                nodes.extend(node.get("Plans", []))
            assert "ix_run_actions_postprocess_run_index" in {
                node.get("Index Name") for node in nodes
            }, nodes
        finally:
            await engine.dispose()

    asyncio.run(exercise())
