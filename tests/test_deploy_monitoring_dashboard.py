"""Review metric queries, database roles and the Grafana dashboard of the monitoring stack."""

from __future__ import annotations

import json
import re
from typing import Any

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.reviews.infrastructure.models import Run
from tests.monitoring_deploy_host import (
    METADATA_GRANTS,
    MONITORING,
    QUERIES,
    _grants,
    _query,
    _read,
)

# Columns that hold review content, diffs or provider bodies: no monitoring role may read them.
CONTENT_COLUMNS = ("request", "response", "response_ref", "review_body", "error_message")


# --- review metrics and roles --------------------------------------------------------------


def test_review_queries_and_grants_never_touch_review_content() -> None:
    sql = "\n".join(re.findall(r"query: \|\n((?:    .*\n)+)", _read(QUERIES)))
    deploy = _read(MONITORING / "deploy.sh")
    grants = _grants(deploy)
    dashboard = json.loads(_read(MONITORING / "grafana" / "dashboards" / "overview.json"))
    dashboard_sql = "\n".join(
        target["rawSql"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "rawSql" in target
    )

    assert sql.count("SELECT") == 6
    assert dashboard_sql.count("SELECT") == 9
    assert {table for table, _ in grants} == {"runs", "run_actions", "usage_events"}
    granted = set().union(*grants.values())
    for column in CONTENT_COLUMNS:
        assert not re.search(rf"\b{column}\b", sql), column
        assert column not in granted, column
        assert not re.search(rf"\b{column}\b", dashboard_sql), column
    # Grants are rebuilt from scratch on every deploy and the roles cannot write.
    assert (
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_reader, metrics_exporter;" in deploy
    )
    assert deploy.count("SET default_transaction_read_only = on;") == 2


def test_exporter_reads_only_the_cost_and_time_of_llm_usage() -> None:
    deploy = _read(MONITORING / "deploy.sh")
    statements = [
        " ".join(statement.split())
        for statement in re.findall(r"\bGRANT\b[^;]*;", deploy, re.IGNORECASE)
        if re.search(r"\b(grafana_reader|metrics_exporter)\b", statement)
    ]
    column_grant = r"GRANT SELECT \([\w, ]+\) ON \w+ TO [\w, ]+;"

    assert _grants(deploy) == METADATA_GRANTS
    # Any other grant to the read-only roles opens no table: a table-wide SELECT, ALL or ON ALL
    # TABLES would bypass the column lists above.
    assert [statement for statement in statements if not re.fullmatch(column_grant, statement)] == [
        "GRANT pg_monitor TO metrics_exporter;",
        'GRANT CONNECT ON DATABASE "${database}" TO grafana_reader, metrics_exporter;',
        "GRANT USAGE ON SCHEMA public TO grafana_reader, metrics_exporter;",
    ]


def test_month_cost_query_sums_the_utc_month_from_cost_and_time_only() -> None:
    block = _read(QUERIES).split("\ndmc_llm_cost_month:\n", 1)[1].split("\n\n", 1)[0]
    sql = _query("dmc_llm_cost_month")
    usage_columns = {column.name for column in UsageEvent.__table__.columns}

    # postgres-exporter names the series <block>_<column>: dmc_llm_cost_month_usd.
    assert "coalesce(sum(cost_usd), 0)::float8 AS usd" in sql
    assert re.search(r"- usd:\n\s+usage: GAUGE\n", block)
    # The three-argument date_trunc ignores the time zone of the session.
    assert "created_at >= date_trunc('month', now(), 'UTC')" in sql
    assert {column for column in usage_columns if re.search(rf"\b{column}\b", sql)} == {
        "cost_usd",
        "created_at",
    }


def test_budget_paused_query_counts_skipped_runs_from_granted_columns() -> None:
    block = _read(QUERIES).split("\ndmc_runs_budget_paused_15m:\n", 1)[1].split("\n\n", 1)[0]
    sql = _query("dmc_runs_budget_paused_15m")
    run_columns = {column.name for column in Run.__table__.columns}
    read = {column for column in run_columns if re.search(rf"\b{column}\b", sql)}

    # postgres-exporter names the series <block>_<column>: dmc_runs_budget_paused_15m_runs.
    assert "SELECT count(*)::float8 AS runs" in sql
    assert re.search(r"- runs:\n\s+usage: GAUGE\n", block)
    assert "state = 'skipped' AND error_code = 'budget_paused'" in sql
    assert "finished_at > now() - interval '15 minutes'" in sql
    # No new grant: the exporter already reads these columns of runs.
    assert read == {"state", "error_code", "finished_at"}
    assert read <= _grants(_read(MONITORING / "deploy.sh"))[("runs", "metrics_exporter")]


def test_review_time_query_measures_succeeded_fast_runs_from_granted_columns() -> None:
    block = _read(QUERIES).split("\ndmc_review_time_1h:\n", 1)[1].split("\n\n", 1)[0]
    sql = _query("dmc_review_time_1h")
    run_columns = {column.name for column in Run.__table__.columns}
    read = {column for column in run_columns if re.search(rf"\b{column}\b", sql)}

    # postgres-exporter names the series <block>_<column>: dmc_review_time_1h_runs and
    # dmc_review_time_1h_p95_seconds.
    assert re.search(r"- runs:\n\s+usage: GAUGE\n", block)
    assert re.search(r"- p95_seconds:\n\s+usage: GAUGE\n", block)
    assert re.findall(r"^\s*(?:FROM|JOIN)\s+(\w+)", sql, re.M) == ["runs"]
    assert "state = 'succeeded' AND engine = 'fast'" in sql
    assert "finished_at > now() - interval '1 hour'" in sql
    # The second bound lets ix_runs_state_created_id serve the scan.
    assert "created_at > now() - interval '1 day'" in sql
    # No new grant: the exporter already reads these columns of runs.
    assert read == {"state", "engine", "finished_at", "created_at"}
    assert read <= _grants(_read(MONITORING / "deploy.sh"))[("runs", "metrics_exporter")]


# --- dashboard -----------------------------------------------------------------------------

DASHBOARD = MONITORING / "grafana" / "dashboards" / "overview.json"
PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}


def _panel_with(metric: str) -> Any:
    dashboard = json.loads(_read(DASHBOARD))
    return next(
        (
            panel
            for panel in dashboard["panels"]
            if any(metric in target.get("expr", "") for target in panel.get("targets", []))
        ),
        None,
    )


def test_dashboard_shows_whether_each_probed_name_is_up() -> None:
    panel = _panel_with("probe_success{")

    assert panel is not None
    assert panel["datasource"] == PROMETHEUS
    [target] = panel["targets"]
    assert target["datasource"] == PROMETHEUS
    assert target["expr"] == 'probe_success{job="blackbox", environment="${environment:text}"}'
    assert target["legendFormat"] == "{{host}}"
    # One value per name: the default text mode would hide which name it is.
    assert panel["options"]["textMode"] == "value_and_name"
    [mapping] = panel["fieldConfig"]["defaults"]["mappings"]
    shown = {value: (text["text"], text["color"]) for value, text in mapping["options"].items()}
    assert shown == {"0": ("DOWN", "red"), "1": ("UP", "green")}


def test_dashboard_shows_certificate_days_coloured_where_the_alerts_fire() -> None:
    panel = _panel_with("probe_ssl_earliest_cert_expiry{")
    rules = _read(MONITORING / "prometheus" / "rules" / "alerts.yml")
    days = {
        alert: int(limit)
        for alert, limit in re.findall(
            r"- alert: (TlsCertificate\w+)\n        expr: .* < (\d+)\n", rules
        )
    }

    assert days.keys() == {"TlsCertificateExpiresSoon", "TlsCertificateExpiring"}
    assert days == {"TlsCertificateExpiresSoon": 14, "TlsCertificateExpiring": 3}
    assert panel is not None
    assert panel["datasource"] == PROMETHEUS
    [target] = panel["targets"]
    assert target["datasource"] == PROMETHEUS
    # The same 10-minute hold as the alerts: a probe without a TLS answer does not blank it.
    assert target["expr"] == (
        "(last_over_time(probe_ssl_earliest_cert_expiry"
        '{job="blackbox", environment="${environment:text}"}[10m]) - time()) / 86400'
    )
    assert target["legendFormat"] == "{{host}}"
    assert panel["options"]["textMode"] == "value_and_name"
    defaults = panel["fieldConfig"]["defaults"]
    # Grafana's own day unit shows 7 days and more as weeks; a plain suffix keeps days.
    assert defaults["unit"] == "suffix:d"
    # One decimal like the alert summaries: 2.6 days must not read as "3 d".
    assert defaults["decimals"] == 1
    assert [(step["color"], step["value"]) for step in defaults["thresholds"]["steps"]] == [
        ("red", None),
        ("orange", days["TlsCertificateExpiring"]),
        ("green", days["TlsCertificateExpiresSoon"]),
    ]


def test_dashboard_shows_month_to_date_llm_spend_next_to_cost_per_hour() -> None:
    panels = json.loads(_read(DASHBOARD))["panels"]
    panel = _panel_with("dmc_llm_cost_month_usd")
    cost_per_hour = next(other for other in panels if other["title"] == "LLM cost per hour")
    rules = _read(MONITORING / "prometheus" / "rules" / "alerts.yml")
    # No budget (or 0) leaves the percentage empty instead of +Inf.
    share = "100 * sum(dmc_llm_cost_month_usd) / (max(dmc_llm_monthly_budget_usd) > 0)"
    alerts = {
        alert: (alert_share, int(limit))
        for alert, alert_share, limit in re.findall(
            r"- alert: (LlmMonthlyBudget\w+)\n        expr: (.*) >= (\d+)(?: < \d+)?\n", rules
        )
    }

    assert alerts.keys() == {"LlmMonthlyBudgetWarning", "LlmMonthlyBudgetExceeded"}
    assert {alert: limit for alert, (_, limit) in alerts.items()} == {
        "LlmMonthlyBudgetWarning": 80,
        "LlmMonthlyBudgetExceeded": 100,
    }
    assert {alert: alert_share for alert, (alert_share, _) in alerts.items()} == {
        "LlmMonthlyBudgetWarning": share,
        "LlmMonthlyBudgetExceeded": share,
    }
    assert panel is not None
    assert panel["title"] == "LLM spend this month"
    assert panel["type"] == "stat"
    # On the row of LLM cost per hour, right next to it.
    assert panel["gridPos"]["y"] == cost_per_hour["gridPos"]["y"]
    assert panel["gridPos"]["h"] == cost_per_hour["gridPos"]["h"]
    assert panel["gridPos"]["x"] == cost_per_hour["gridPos"]["x"] + cost_per_hour["gridPos"]["w"]
    # Prometheus, not SQL: the same sum over the scraped databases as the alerts.
    assert panel["datasource"] == PROMETHEUS
    targets = {target["refId"]: target for target in panel["targets"]}
    assert targets.keys() == {"A", "B"}
    for target in targets.values():
        assert target["datasource"] == PROMETHEUS
        assert "rawSql" not in target
        assert "dmc_llm_cost_month_usd" in target["expr"]
    # The current value, not the range: a stat shows the latest scrape.
    assert all(target["instant"] and not target["range"] for target in targets.values())
    assert panel["options"]["reduceOptions"]["calcs"] == ["lastNotNull"]
    assert targets["A"]["expr"] == "sum(dmc_llm_cost_month_usd)"
    assert targets["B"]["expr"] == share
    # Two values in one panel: the default text mode would hide which one is which.
    assert panel["options"]["textMode"] == "value_and_name"
    defaults = panel["fieldConfig"]["defaults"]
    assert defaults["unit"] == "currencyUSD"
    # The budget colours belong to the percentage only: spend of 85 USD is not orange.
    assert [step["value"] for step in defaults["thresholds"]["steps"]] == [None]
    [override] = panel["fieldConfig"]["overrides"]
    assert override["matcher"] == {"id": "byFrameRefID", "options": "B"}
    percent = {prop["id"]: prop["value"] for prop in override["properties"]}
    assert percent["unit"] == "percent"
    # The rounding of the alert summaries (%.2f USD, %.2f %): 99.96 % must not read as 100 %.
    assert (defaults["decimals"], percent["decimals"]) == (2, 2)
    assert [(step["color"], step["value"]) for step in percent["thresholds"]["steps"]] == [
        ("green", None),
        ("orange", alerts["LlmMonthlyBudgetWarning"][1]),
        ("red", alerts["LlmMonthlyBudgetExceeded"][1]),
    ]


def test_dashboard_panels_have_unique_ids_and_never_overlap() -> None:
    panels = json.loads(_read(DASHBOARD))["panels"]
    ids = [panel["id"] for panel in panels]
    ids += [nested["id"] for panel in panels for nested in panel.get("panels", [])]
    boxes = [
        (panel["gridPos"]["x"], panel["gridPos"]["y"], panel["gridPos"]["w"], panel["gridPos"]["h"])
        for panel in panels
    ]

    assert len(ids) == len(set(ids))
    for i, (x, y, w, h) in enumerate(boxes):
        for x2, y2, w2, h2 in boxes[i + 1 :]:
            assert x + w <= x2 or x2 + w2 <= x or y + h <= y2 or y2 + h2 <= y, (ids[i], x2, y2)
