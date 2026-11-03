"""Review metric queries of postgres-exporter on PostgreSQL (deploy/monitoring/postgres-exporter).

The SQL is read from queries.yml and runs on the migrated schema, so a query error shows here and
not as a metric that silently disappears from Prometheus. Opt-in: set ``TEST_DATABASE_URL`` to a
disposable PostgreSQL database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

from tests.monitoring_deploy_host import MONITORING, _grants, _query, _read
from tests.portal_postgres import PR_OPEN, PROMPT, RULE_A, RUN_DONE, WS_A, Env, portal_schema

# UTC+14 all year: its calendar month starts 14 hours before the UTC one.
FAR_EAST = "Pacific/Kiritimati"


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


@pytest.fixture
def connection(env: Env) -> Iterator[Connection]:
    """One transaction per test, rolled back: now() is the same for the rows and the query."""
    engine = create_engine(
        env.database_url,
        connect_args={"options": f"-csearch_path={env.schema}"},
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            yield connection
            connection.rollback()
    finally:
        engine.dispose()


def _usage(connection: Connection, created_at: datetime, cost: str) -> None:
    connection.execute(
        text(
            "INSERT INTO usage_events (id, run_id, workspace_id, provider, model, operation, "
            "tokens_in, tokens_out, cost_usd, created_at) "
            "VALUES (:id, :run, :workspace, 'eurouter', 'model', 'review', 100, 10, :cost, "
            ":created_at)"
        ),
        {
            "id": uuid4(),
            "run": RUN_DONE,
            "workspace": WS_A,
            "cost": Decimal(cost),
            "created_at": created_at,
        },
    )


def _run(connection: Connection, state: str, error_code: str, finished_at: datetime) -> None:
    connection.execute(
        text(
            "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, state, trigger, "
            "idempotency_key, engine, rule_version_id, prompt_version_id, available_at, attempt, "
            "created_at, error_code, finished_at) VALUES (:id, :pr, :sha, 'main', :sha, :state, "
            "'manual', :key, 'fast', :rule, :prompt, :at, 1, :at, :error_code, :at)"
        ),
        {
            "id": uuid4(),
            "pr": PR_OPEN,
            "sha": "c" * 40,
            "state": state,
            "key": uuid4().hex * 2,
            "rule": RULE_A,
            "prompt": PROMPT,
            "at": finished_at,
            "error_code": error_code,
        },
    )


def _metric(connection: Connection, name: str) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in connection.execute(text(_query(name)))]


def _as_exporter(connection: Connection) -> None:
    """Switch the transaction to a new role with exactly the exporter's column grants.

    Role names are cluster-wide: a unique one, created in the transaction and rolled back with it.
    """
    role = f"monitoring_probe_{uuid4().hex}"
    schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    grants = _grants(_read(MONITORING / "deploy.sh"))
    connection.execute(text(f"CREATE ROLE {role}"))
    connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}'))
    for (table, grantee), columns in sorted(grants.items()):
        if grantee == "metrics_exporter":
            connection.execute(
                text(f"GRANT SELECT ({', '.join(sorted(columns))}) ON {table} TO {role}")
            )
    connection.execute(text(f"SET LOCAL ROLE {role}"))


def _month_start(connection: Connection) -> tuple[datetime, datetime]:
    now = connection.execute(text("SELECT now()")).scalar_one().astimezone(UTC)
    return now, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.integration
def test_month_cost_starts_at_the_utc_month_whatever_the_session_time_zone(
    connection: Connection,
) -> None:
    connection.execute(text(f"SET LOCAL TIME ZONE '{FAR_EAST}'"))
    now, month_start = _month_start(connection)
    _usage(connection, month_start - timedelta(hours=1), "100")
    _usage(connection, month_start, "1.25")
    _usage(connection, now, "2.5")
    local_month = connection.execute(text("SELECT date_trunc('month', now())")).scalar_one()

    # The session month is not the UTC one, so a query in session time would count otherwise.
    assert local_month != month_start
    assert _metric(connection, "dmc_llm_cost_month") == [{"usd": 3.75}]
    # The columns deploy.sh grants to metrics_exporter are enough for the query.
    _as_exporter(connection)
    assert _metric(connection, "dmc_llm_cost_month") == [{"usd": 3.75}]


@pytest.mark.integration
def test_month_cost_is_zero_before_the_first_call_of_the_month(connection: Connection) -> None:
    _, month_start = _month_start(connection)
    _usage(connection, month_start - timedelta(microseconds=1), "7")

    assert _metric(connection, "dmc_llm_cost_month") == [{"usd": 0.0}]


@pytest.mark.integration
def test_budget_paused_counts_runs_skipped_by_the_budget_in_the_last_15_minutes(
    connection: Connection,
) -> None:
    now = connection.execute(text("SELECT now()")).scalar_one()
    window = timedelta(minutes=15)
    # The window is open at its start: a run finished exactly 15 minutes ago is out.
    _run(connection, "skipped", "budget_paused", now - window)

    assert _metric(connection, "dmc_runs_budget_paused_15m") == [{"runs": 0.0}]

    _run(connection, "skipped", "budget_paused", now - window + timedelta(seconds=1))
    _run(connection, "skipped", "budget_paused", now)
    # Another skip reason, and a failed run even with this code, do not count.
    _run(connection, "skipped", "repo_disabled", now)
    _run(connection, "failed", "budget_paused", now)

    assert _metric(connection, "dmc_runs_budget_paused_15m") == [{"runs": 2.0}]
    # The columns deploy.sh already grants to metrics_exporter are enough for the query.
    _as_exporter(connection)
    assert _metric(connection, "dmc_runs_budget_paused_15m") == [{"runs": 2.0}]
