"""Deployment contracts of the monitoring stack (deploy/monitoring, docs/MONITORING.md).

The static tests read the compose files, the Caddyfile and the workflow like the staging tests do.
The behavioural tests run the real deploy/monitoring/deploy.sh against a fake docker CLI: what it
writes on the host, what reaches PostgreSQL and what never appears in its output, without a VPS or
a Docker daemon. Prometheus rules and the Alertmanager config are checked by promtool and amtool in
the CI job monitoring-validate (scripts/validate_monitoring.sh).
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.reviews.infrastructure.models import Run

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITORING = REPO_ROOT / "deploy" / "monitoring"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"
CADDYFILE = REPO_ROOT / "deploy" / "edge" / "Caddyfile"
API_PROJECT = "dmc-268-api-staging"
TOKEN = "123456:TEST-bot_token"
CHAT_ID = "-1001234567890"
# Columns that hold review content, diffs or provider bodies: no monitoring role may read them.
CONTENT_COLUMNS = ("request", "response", "response_ref", "review_body", "error_message")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _service_blocks(compose: str) -> dict[str, str]:
    services = compose[compose.index("services:\n") :]
    services = services[: services.index("\nvolumes:\n")]
    names = list(re.finditer(r"^  ([a-z-]+):$", services, re.M))
    return {
        match.group(1): services[
            match.start() : (names[i + 1].start() if i + 1 < len(names) else None)
        ]
        for i, match in enumerate(names)
    }


def _job(name: str) -> str:
    workflow = _read(WORKFLOW)
    start = workflow.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z-]+:\n", workflow[start + 1 :])
    return workflow[start : start + 1 + following.start() if following else None]


# --- compose -------------------------------------------------------------------------------


def test_monitoring_images_are_pinned_by_digest() -> None:
    images = re.findall(r"^    image: (\S+)$", _read(MONITORING / "compose.yml"), re.M)

    assert len(images) == 11
    for image in images:
        assert re.fullmatch(r"[a-z0-9./_-]+:v?[\d.]+(-alpine)?@sha256:[0-9a-f]{64}", image), image


def test_backup_uses_the_postgres_image_of_staging() -> None:
    # pg_dump must match the server version: the same image, by the same digest.
    staging = re.search(
        r"^    image: (postgres:\S+)$", _read(REPO_ROOT / "deploy/compose/staging.yml"), re.M
    )
    backup = _service_blocks(_read(MONITORING / "compose.yml"))["pg-backup"]

    assert staging is not None
    assert f"    image: {staging.group(1)}\n" in backup


def test_every_monitoring_service_has_a_memory_limit() -> None:
    for name, block in _service_blocks(_read(MONITORING / "compose.yml")).items():
        assert re.search(r"^    mem_limit: \d+m$", block, re.M), name


def test_monitoring_publishes_no_port_except_loopback_grafana() -> None:
    assert "ports:" not in _read(MONITORING / "compose.yml")
    assert "ports:" not in _read(MONITORING / "compose.edge.yml")
    published = re.findall(r'^      - "(.+)"$', _read(MONITORING / "compose.ports.yml"), re.M)

    assert published == ["127.0.0.1:3000:3000"]


def test_monitoring_reads_the_application_network_without_joining_its_project() -> None:
    compose = _read(MONITORING / "compose.yml")
    blocks = _service_blocks(compose)

    assert "    name: ${API_NETWORK:?API_NETWORK is required}\n    external: true" in compose
    # Only what talks to postgres, rabbitmq or redis joins the application network, and Tempo,
    # which the application sends its traces to.
    on_api = {name for name, block in blocks.items() if "      api: {}" in block}
    assert on_api == {
        "prometheus",
        "grafana",
        "postgres-exporter",
        "redis-exporter",
        "pg-backup",
        "tempo",
    }
    # The backup job needs PostgreSQL only.
    assert "default: {}" not in blocks["pg-backup"]
    # Host collectors never reach the stores or the edge.
    for name in ("node-exporter", "cadvisor"):
        assert "networks:" not in blocks[name]


def test_only_grafana_prometheus_and_the_status_page_join_the_edge_network() -> None:
    edge = _read(MONITORING / "compose.edge.yml")

    assert set(re.findall(r"^  ([a-z-]+):$", edge, re.M)) == {
        "grafana",
        "prometheus",
        "status-page",
        "edge",
    }
    assert "          - ${GRAFANA_ALIAS:?GRAFANA_ALIAS is required}" in edge


def test_probes_run_from_the_project_network_only() -> None:
    # The probes reach the public names from outside: no store and no edge network, as nobody.
    block = _service_blocks(_read(MONITORING / "compose.yml"))["blackbox-exporter"]

    assert "networks:" not in block
    for overlay in MONITORING.glob("compose.*.yml"):
        assert "blackbox-exporter" not in _read(overlay), overlay.name
    assert '    user: "65534:65534"\n' in block
    assert re.search(r"^    mem_limit: \d+m$", block, re.M)
    assert "      - ./blackbox:/etc/blackbox_exporter:ro\n" in block
    assert "      - --config.file=/etc/blackbox_exporter/blackbox.yml\n" in block
    assert '"http://127.0.0.1:9115/-/healthy"]' in block


def test_the_status_page_is_a_hardened_stdlib_service_on_the_project_and_edge_networks() -> None:
    # Public status page (api#309): the one monitoring service the internet reaches, through Caddy.
    compose = _read(MONITORING / "compose.yml")
    block = _service_blocks(compose)["status-page"]
    edge = _service_blocks(_read(MONITORING / "compose.edge.yml") + "\nvolumes:\n")["status-page"]

    # Pinned like the other images, a patch tag of the alpine variant: no build, no pip.
    assert re.search(r"^    image: python:3\.13\.\d+-alpine@sha256:[0-9a-f]{64}$", block, re.M)
    assert '    command: ["python", "/app/status_page.py"]\n' in block
    assert "      - ./status:/app:ro\n" in block
    assert "      PROMETHEUS_URL: http://prometheus:9090\n" in block
    # Nobody, read-only root, no capability, no privilege gain: as the edge proxy.
    assert '    user: "65534:65534"\n' in block
    assert "    read_only: true\n" in block
    assert "    cap_drop: [ALL]\n" in block
    assert "    security_opt: [no-new-privileges:true]\n" in block
    assert '      PYTHONDONTWRITEBYTECODE: "1"\n' in block
    # It reads Prometheus on the project network and never the stores of the application.
    assert "      default: {}\n" in block
    assert "api: {}" not in block
    assert "ports:" not in block
    assert '"http://127.0.0.1:8080/"]' in block
    # The edge overlay adds the network and the alias of the contract <service>-<env>.
    assert "      default: {}\n      edge:\n        aliases:\n          - status-staging\n" in edge
    assert "status-page" not in _read(MONITORING / "compose.ports.yml")


def test_prometheus_probes_the_public_names_through_the_exporter_once_a_minute() -> None:
    config = _read(MONITORING / "prometheus" / "prometheus.yml")
    job = config[config.index("  - job_name: blackbox\n") :]
    job = job[: job.index("\n\n")] if "\n\n" in job else job
    module = re.search(
        r"^  https_2xx:\n    prober: http\n    timeout: (\d+)s$",
        _read(MONITORING / "blackbox" / "blackbox.yml"),
        re.M,
    )
    scrape_timeout = re.search(r"^    scrape_timeout: (\d+)s$", job, re.M)

    assert "    scrape_interval: 1m\n" in job
    assert "    metrics_path: /probe\n" in job
    assert "      module: [https_2xx]\n" in job
    assert '      - files: ["/etc/prometheus/targets/probes.json"]' in job
    # The probed URL becomes the target parameter and the instance; the exporter is scraped.
    assert "      - source_labels: [__address__]\n        target_label: __param_target\n" in job
    assert "      - source_labels: [__param_target]\n        target_label: instance\n" in job
    assert "      - target_label: __address__\n        replacement: blackbox-exporter:9115" in job
    # A slow name ends the probe as probe_success 0 before the scrape itself times out.
    assert module is not None
    assert scrape_timeout is not None
    assert int(module.group(1)) < int(scrape_timeout.group(1))


def test_prometheus_drops_a_stopped_container_at_the_next_scrape() -> None:
    config = _read(MONITORING / "prometheus" / "prometheus.yml")
    job = config[config.index("  - job_name: cadvisor\n") :]
    job = job[: job.index("\n\n")]

    # cAdvisor timestamps its samples: without stale markers a stopped container lingers 5 minutes.
    assert "    track_timestamps_staleness: true\n" in job
    assert "honor_timestamps: false" not in config


def test_config_changes_recreate_the_service_that_reads_them() -> None:
    blocks = _service_blocks(_read(MONITORING / "compose.yml"))
    digests = {
        "prometheus": "PROMETHEUS_CONFIG_DIGEST",
        "alertmanager": "ALERTMANAGER_CONFIG_DIGEST",
        "grafana": "GRAFANA_CONFIG_DIGEST",
        "postgres-exporter": "POSTGRES_EXPORTER_CONFIG_DIGEST",
        "pg-backup": "PG_BACKUP_CONFIG_DIGEST",
        "blackbox-exporter": "BLACKBOX_CONFIG_DIGEST",
        "tempo": "TEMPO_CONFIG_DIGEST",
        "status-page": "STATUS_CONFIG_DIGEST",
    }
    deploy = _read(MONITORING / "deploy.sh")

    for service, variable in digests.items():
        assert f"dmc.config-digest: ${{{variable}:-none}}" in blocks[service]
        assert f"printf '{variable}=%s\\n'" in deploy


def test_the_application_sends_traces_to_tempo_inside_the_docker_network() -> None:
    blocks = _service_blocks(_read(REPO_ROOT / "deploy/compose/staging.yml"))
    tempo = _read(MONITORING / "tempo" / "tempo.yml")
    datasources = _read(MONITORING / "grafana/provisioning/datasources/datasources.yml")

    for service in ("api", "worker", "webhook-worker"):
        assert "      OTEL_EXPORTER_OTLP_ENDPOINT: http://tempo:4318\n" in blocks[service], service
    # OTLP/HTTP only; no gRPC receiver, no host port (test above), 72 h of traces.
    assert "        http:\n          endpoint: 0.0.0.0:4318\n" in tempo
    assert "grpc" not in tempo
    assert "block_retention: 72h" in tempo
    assert "  - name: Tempo\n    uid: tempo\n    type: tempo\n" in datasources
    assert "    url: http://tempo:3200\n" in datasources


# --- edge proxy ----------------------------------------------------------------------------


def test_edge_exposes_request_metrics_on_an_internal_port_only() -> None:
    caddyfile = _read(CADDYFILE)
    global_options = caddyfile[: caddyfile.index("\n}\n")]
    edge_ports = re.findall(r'^      - "(.+)"$', _read(REPO_ROOT / "deploy/edge/compose.yml"), re.M)

    assert "\tmetrics {\n\t\tper_host\n\t}" in global_options
    assert "http://:9180 {\n\tmetrics /metrics\n}" in caddyfile
    assert edge_ports == ["80:80", "443:443", "443:443/udp"]
    # The admin API stays on localhost: nothing rebinds it.
    assert not re.search(r"^\s*admin\s", caddyfile, re.M)


def test_grafana_has_its_own_host_and_prometheus_none() -> None:
    caddyfile = _read(CADDYFILE)
    site = caddyfile[caddyfile.index("grafana.{$APP_DOMAIN} {") :]
    site = site[: site.index("\n}\n")]
    api_site = caddyfile[caddyfile.index("staging-api.{$APP_DOMAIN} {") :]
    api_site = api_site[: api_site.index("\n}\n")]

    assert re.findall(r"reverse_proxy (\S+)", site) == ["grafana-staging:3000"]
    assert "import hsts" in site
    # /metrics of Grafana needs no login: answered 404 at the edge before the proxy route.
    blocked = site.index(
        "@metrics path /metrics /metrics/*\n\thandle @metrics {\n\t\trespond 404\n\t}"
    )
    assert blocked < site.index("reverse_proxy grafana-staging:3000")
    # The API host is the API only; Prometheus has no authentication and gets no public site.
    assert re.findall(r"reverse_proxy (\S+)", api_site) == ["api-staging:8000"]
    assert not re.search(r"^(prometheus|victoria-metrics)\.", caddyfile, re.M)
    assert "GRAFANA_ROOT_URL: https://grafana.${{ vars.APP_DOMAIN }}/" in _job("deploy-monitoring")


# --- review metrics and roles --------------------------------------------------------------

QUERIES = MONITORING / "postgres-exporter" / "queries.yml"
RUN_COLUMNS = {
    "id",
    "state",
    "trigger",
    "engine",
    "attempt",
    "error_code",
    "available_at",
    "created_at",
    "started_at",
    "finished_at",
}
RUN_ACTION_COLUMNS = {"run_id", "tool", "started_at", "duration_ms"}
# The column grants of the read-only roles, by table and role: all they can read.
METADATA_GRANTS = {
    ("runs", "grafana_reader"): RUN_COLUMNS,
    ("runs", "metrics_exporter"): RUN_COLUMNS,
    ("run_actions", "grafana_reader"): RUN_ACTION_COLUMNS,
    ("run_actions", "metrics_exporter"): RUN_ACTION_COLUMNS,
    ("usage_events", "grafana_reader"): {
        "provider",
        "model",
        "operation",
        "tokens_in",
        "tokens_out",
        "cache_read_tokens",
        "cost_usd",
        "created_at",
    },
    ("usage_events", "metrics_exporter"): {"cost_usd", "created_at"},
}


def _query(name: str) -> str:
    """The SQL of one postgres-exporter query block of queries.yml."""
    block = re.search(rf"^{name}:\n  query: \|\n((?:    .*\n)+)", _read(QUERIES), re.M)
    assert block is not None, name
    return block.group(1)


def _grants(sql: str) -> dict[tuple[str, str], set[str]]:
    """Column grants by table and recipient role: GRANT SELECT (columns) ON table TO roles."""
    statements = re.findall(r"GRANT SELECT \((.*?)\)\s+ON (\w+) TO ([^;]+);", sql, re.DOTALL)
    assert len(statements) == sql.count("GRANT SELECT ("), "a column grant was not read"
    grants: dict[tuple[str, str], set[str]] = {}
    for columns, table, roles in statements:
        for role in roles.split(","):
            grants.setdefault((table, role.strip()), set()).update(
                column.strip() for column in columns.split(",")
            )
    return grants


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

    assert sql.count("SELECT") == 5
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


# --- workflow ------------------------------------------------------------------------------


def test_push_image_waits_for_the_monitoring_validation() -> None:
    job = _job("monitoring-validate")

    assert "run: scripts/validate_monitoring.sh" in job
    assert "continue-on-error" not in job
    assert "- monitoring-validate" in _job("push-image")


def test_monitoring_deploys_after_the_application_and_never_gates_it() -> None:
    job = _job("deploy-monitoring")

    assert "    needs: deploy-staging\n" in job
    assert "needs.deploy-staging.result == 'success'" in job
    assert "github.ref == 'refs/heads/main'" in job
    assert "rollback" not in job.lower()
    # Promotion depends on the application deploy only.
    assert "deploy-monitoring" not in _job("promote-staging")
    # Its own group: in staging-deploy it would hold the VPS lock for the monitoring deploy and
    # delay promote-staging and every queued member behind it; it only reads the app stack.
    assert re.findall(r"^      group: (\S+)$", job, re.M) == ["staging-monitoring"]


def test_monitoring_upload_and_secrets_reach_only_the_monitoring_dir() -> None:
    job = _job("deploy-monitoring")

    assert (
        'source: "deploy/monitoring,deploy/scripts/env-file.sh,deploy/scripts/check-isolation.sh"'
        in job
    )
    assert "target: ${{ env.MONITORING_DIR }}" in job
    assert "ALERT_TELEGRAM_BOT_TOKEN: ${{ secrets.ALERT_TELEGRAM_BOT_TOKEN }}" in job
    assert "ALERT_TELEGRAM_CHAT_ID: ${{ vars.ALERT_TELEGRAM_CHAT_ID }}" in job
    assert "ALERT_TELEGRAM" not in _job("deploy-staging")


def test_monitoring_deploy_gets_the_domain_of_the_probed_names() -> None:
    job = _job("deploy-monitoring")
    envs = re.search(r"^          envs: (\S+)$", job, re.M)

    assert "          APP_DOMAIN: ${{ vars.APP_DOMAIN }}\n" in job
    assert envs is not None
    assert "APP_DOMAIN" in envs.group(1).split(",")


def test_monitoring_deploy_gets_the_production_names_to_probe() -> None:
    job = _job("deploy-monitoring")
    envs = re.search(r"^          envs: (\S+)$", job, re.M)

    assert "          PROBE_PROD_HOSTS: ${{ vars.PROBE_PROD_HOSTS }}\n" in job
    assert envs is not None
    assert "PROBE_PROD_HOSTS" in envs.group(1).split(",")
    assert "PROBE_PROD_HOSTS" not in _job("deploy-staging")


def test_monitoring_deploy_gets_the_monthly_llm_budget() -> None:
    job = _job("deploy-monitoring")
    envs = re.search(r"^          envs: (\S+)$", job, re.M)

    assert "          LLM_MONTHLY_BUDGET_USD: ${{ vars.LLM_MONTHLY_BUDGET_USD }}\n" in job
    assert envs is not None
    assert "LLM_MONTHLY_BUDGET_USD" in envs.group(1).split(",")
    assert "LLM_MONTHLY_BUDGET_USD" not in _job("deploy-staging")


# --- deploy.sh on a fake host --------------------------------------------------------------

FAKE_DOCKER = """\
#!/usr/bin/env bash
# Fake docker CLI: logs every call; psql, grafana and rclone obscure stdin go to their own logs.
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  network) [[ " $* " == *" ${STUB_MISSING_NETWORK:-none} "* ]] && exit 1; exit 0 ;;
  run) cat >> "${STUB_OBSCURE_STDIN}"; echo "obscured_${RANDOM}${RANDOM}" ;;
  ps)
    case "$*" in
      *com.docker.compose.project=dmc-268-api-prod*) echo "${STUB_PROD_POSTGRES_ID}" ;;
      *) echo "${STUB_POSTGRES_ID}" ;;
    esac
    ;;
  exec)
    printf -- '-- container %s\\n' "$3" >> "${STUB_PSQL_STDIN}"
    cat >> "${STUB_PSQL_STDIN}"; printf '\\n--\\n' >> "${STUB_PSQL_STDIN}"
    if [[ "$3" == "${STUB_EXEC_FAILS:-none}" ]]; then exit 1; fi
    ;;
  compose)
    if [[ " $* " == *" exec "* ]]; then cat >> "${STUB_GRAFANA_STDIN}"; fi
    if [[ " $* " == *" up "* && -n "${STUB_UP_FAILS:-}" ]]; then exit 1; fi
    ;;
esac
"""
FAKE_CHOWN = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_CHOWN_LOG}"
"""


@dataclass
class Host:
    root: Path
    api_dir: Path
    monitoring_dir: Path
    stubs: Path

    @property
    def env_file(self) -> Path:
        return self.monitoring_dir / ".env"

    def env(self) -> dict[str, str]:
        values = {}
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return values

    def log(self, name: str) -> str:
        path = self.stubs / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def deploy(
        self,
        *,
        unset: tuple[str, ...] = (),
        extra: dict[str, str] | None = None,
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "API_DIR": str(self.api_dir),
            "GRAFANA_ROOT_URL": "https://grafana.example.test/",
            "APP_DOMAIN": "example.test",
            # An unset Environment variable reaches the host as an empty value.
            "PROBE_PROD_HOSTS": "",
            # So does an unset repository variable.
            "LLM_MONTHLY_BUDGET_USD": "",
            "ALERT_TELEGRAM_BOT_TOKEN": TOKEN,
            "ALERT_TELEGRAM_CHAT_ID": CHAT_ID,
            "STUB_LOG": str(self.stubs / "docker.log"),
            "STUB_PSQL_STDIN": str(self.stubs / "psql.log"),
            "STUB_GRAFANA_STDIN": str(self.stubs / "grafana.log"),
            "STUB_CHOWN_LOG": str(self.stubs / "chown.log"),
            "STUB_POSTGRES_ID": "pgcontainer",
            "STUB_PROD_POSTGRES_ID": "pgcontainer-prod",
            "STUB_OBSCURE_STDIN": str(self.stubs / "obscure.log"),
            "BACKUP_DIR": str(self.root / "backups"),
            **(extra or {}),
            **overrides,
        }
        for name in unset:
            env.pop(name, None)
        return subprocess.run(
            [str(self.monitoring_dir / "deploy.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """A host after the application deploy: its .env and env-file.sh, the uploaded stack."""
    api_dir = tmp_path / "opt" / API_PROJECT
    api_dir.mkdir(parents=True)
    (api_dir / ".env").write_text(
        "POSTGRES_USER=app\nPOSTGRES_PASSWORD=pg-secret\nPOSTGRES_DB=app\n"
        "DEPLOY_MODE=edge\nEDGE_ALIAS=api-staging\nREDIS_PASSWORD=redis-pa$$word\n",
        encoding="utf-8",
    )
    monitoring_dir = tmp_path / "opt" / "dmc-268-monitoring"
    subprocess.run(["cp", "-R", f"{MONITORING}/.", str(monitoring_dir)], check=True)
    for generated in (".env", "alertmanager/alertmanager.yml", "prometheus/targets/probes.json"):
        (monitoring_dir / generated).unlink(missing_ok=True)
    subprocess.run(["rm", "-rf", str(monitoring_dir / "alertmanager" / "secrets")], check=True)
    (monitoring_dir / "env-file.sh").write_text(
        _read(REPO_ROOT / "deploy" / "scripts" / "env-file.sh"), encoding="utf-8"
    )
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, source in (("docker", FAKE_DOCKER), ("chown", FAKE_CHOWN)):
        (stubs / name).write_text(source, encoding="utf-8")
        (stubs / name).chmod(0o755)
    return Host(tmp_path, api_dir, monitoring_dir, stubs)


def test_first_deploy_generates_stack_passwords_once(host: Host) -> None:
    first = host.deploy()
    assert first.returncode == 0, first.stderr
    passwords = {key: value for key, value in host.env().items() if key.endswith("_PASSWORD")}
    second = host.deploy()

    assert second.returncode == 0, second.stderr
    assert set(passwords) == {
        "REDIS_PASSWORD",
        "GRAFANA_ADMIN_PASSWORD",
        "GRAFANA_DB_PASSWORD",
        "PG_EXPORTER_PASSWORD",
        "PG_BACKUP_PASSWORD",
    }
    for key in (
        "GRAFANA_ADMIN_PASSWORD",
        "GRAFANA_DB_PASSWORD",
        "PG_EXPORTER_PASSWORD",
        "PG_BACKUP_PASSWORD",
    ):
        assert re.fullmatch(r"[0-9a-f]{48}", passwords[key]), key
        assert host.env()[key] == passwords[key], key
    # The Redis password of the application, escaped for compose like its own .env.
    assert passwords["REDIS_PASSWORD"] == "redis-pa$$word"
    assert "generated" not in second.stderr
    assert stat.S_IMODE(host.env_file.stat().st_mode) == 0o600
    assert host.env()["API_NETWORK"] == f"{API_PROJECT}_default"


def test_passwords_reach_postgres_and_grafana_through_stdin_only(host: Host) -> None:
    result = host.deploy()
    env = host.env()
    secrets = (
        env["GRAFANA_ADMIN_PASSWORD"],
        env["GRAFANA_DB_PASSWORD"],
        env["PG_EXPORTER_PASSWORD"],
        env["PG_BACKUP_PASSWORD"],
        TOKEN,
    )

    assert result.returncode == 0, result.stderr
    assert f"ALTER ROLE grafana_reader PASSWORD '{env['GRAFANA_DB_PASSWORD']}';" in host.log(
        "psql.log"
    )
    assert f"ALTER ROLE metrics_exporter PASSWORD '{env['PG_EXPORTER_PASSWORD']}';" in host.log(
        "psql.log"
    )
    assert f"ALTER ROLE backup_agent PASSWORD '{env['PG_BACKUP_PASSWORD']}';" in host.log(
        "psql.log"
    )
    assert host.log("grafana.log") == env["GRAFANA_ADMIN_PASSWORD"]
    for secret in secrets:
        assert secret not in host.log("docker.log")  # argv of every docker call
        assert secret not in result.stdout + result.stderr
    assert "exec -i pgcontainer psql -X -q -v ON_ERROR_STOP=1 -U app -d app" in host.log(
        "docker.log"
    )


# Characters a password manager generates; $ is stored as $$ for compose.
GRAFANA_SECRET = "Team-Graf@na_pass!2026%x&y$z(=)*"


def test_grafana_admin_password_from_the_secret_is_kept_when_the_secret_is_empty(
    host: Host,
) -> None:
    generated = host.deploy()
    assert generated.returncode == 0, generated.stderr
    from_secret = host.deploy(GRAFANA_ADMIN_PASSWORD=GRAFANA_SECRET)
    stored = host.env()["GRAFANA_ADMIN_PASSWORD"]
    # The deploy job passes an empty value when the secret is not set.
    kept = host.deploy(GRAFANA_ADMIN_PASSWORD="")

    assert from_secret.returncode == 0, from_secret.stderr
    assert stored == GRAFANA_SECRET.replace("$", "$$")
    assert kept.returncode == 0, kept.stderr
    assert host.env()["GRAFANA_ADMIN_PASSWORD"] == stored
    # Synced through stdin on every deploy, never in argv or the job log.
    assert host.log("grafana.log").endswith(GRAFANA_SECRET * 2)
    assert GRAFANA_SECRET not in host.log("docker.log")
    for result in (from_secret, kept):
        assert GRAFANA_SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "password",
    [
        "short-pass$word",
        "with space inside it",
        'has"double"quotes-here',
        "has'single-quote-here",
        "has\\backslash-in-the-middle",
        "has`backtick-in-the-middle",
        "has#hash-in-the-middle-x",
    ],
)
def test_invalid_grafana_admin_password_fails_before_the_host_changes(
    host: Host, password: str
) -> None:
    result = host.deploy(GRAFANA_ADMIN_PASSWORD=password)

    assert result.returncode == 1
    assert "GRAFANA_ADMIN_PASSWORD must be 16-128 printable characters" in result.stderr
    assert not host.env_file.exists()
    assert host.log("psql.log") == ""
    assert password not in result.stdout + result.stderr


def test_invalid_grafana_admin_password_in_env_is_named_not_regenerated(host: Host) -> None:
    assert host.deploy().returncode == 0
    env = host.env_file.read_text(encoding="utf-8")
    host.env_file.write_text(
        re.sub(
            r"^GRAFANA_ADMIN_PASSWORD=.*$", "GRAFANA_ADMIN_PASSWORD=edited by hand", env, flags=re.M
        ),
        encoding="utf-8",
    )

    result = host.deploy(GRAFANA_ADMIN_PASSWORD="")

    assert result.returncode == 1
    assert "GRAFANA_ADMIN_PASSWORD in" in result.stderr
    assert "is not a valid password: set the secret" in result.stderr
    assert "edited by hand" not in result.stderr


def test_grafana_admin_password_check_does_not_depend_on_the_locale(host: Host) -> None:
    # 16 characters, one of them non-ASCII: rejected under any locale of the host.
    for locale in ("C", "C.UTF-8"):
        result = host.deploy(GRAFANA_ADMIN_PASSWORD="pässword-1234567", LC_ALL=locale)
        assert result.returncode == 1, locale
        assert "GRAFANA_ADMIN_PASSWORD must be" in result.stderr


def test_telegram_token_lives_in_a_file_read_by_alertmanager_only(host: Host) -> None:
    result = host.deploy()
    token_file = host.monitoring_dir / "alertmanager" / "secrets" / "telegram_bot_token"
    config = _read(host.monitoring_dir / "alertmanager" / "alertmanager.yml")

    assert result.returncode == 0, result.stderr
    assert token_file.read_text(encoding="utf-8") == TOKEN
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o400
    assert f"65534:65534 {token_file}.tmp" in host.log("chown.log")
    assert "bot_token_file: /etc/alertmanager/secrets/telegram_bot_token" in config
    assert f"chat_id: {CHAT_ID}" in config
    assert "  receiver: telegram" in config
    assert TOKEN not in config
    assert TOKEN not in _read(host.env_file)
    assert "alerts: Telegram chat configured" in result.stdout


def test_without_telegram_settings_alerts_are_not_delivered(host: Host) -> None:
    assert host.deploy().returncode == 0
    result = host.deploy(ALERT_TELEGRAM_BOT_TOKEN="", ALERT_TELEGRAM_CHAT_ID="")
    config = _read(host.monitoring_dir / "alertmanager" / "alertmanager.yml")

    assert result.returncode == 0, result.stderr
    assert "  receiver: none" in config
    assert "telegram_configs" not in config
    assert not (host.monitoring_dir / "alertmanager" / "secrets" / "telegram_bot_token").exists()
    assert "alerts are not delivered" in result.stderr


def test_manual_run_without_settings_keeps_the_previous_alert_config(host: Host) -> None:
    assert host.deploy().returncode == 0
    result = host.deploy(unset=("ALERT_TELEGRAM_BOT_TOKEN", "ALERT_TELEGRAM_CHAT_ID"))

    assert result.returncode == 0, result.stderr
    assert "  receiver: telegram" in _read(
        host.monitoring_dir / "alertmanager" / "alertmanager.yml"
    )


@pytest.mark.parametrize(
    ("token", "chat_id", "message"),
    [
        (TOKEN, "", "set both ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID, or neither"),
        ("", CHAT_ID, "set both ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID, or neither"),
        ("not-a-token", CHAT_ID, "ALERT_TELEGRAM_BOT_TOKEN is not a Telegram bot token"),
        (TOKEN, "@team", "ALERT_TELEGRAM_CHAT_ID must be a numeric Telegram chat id"),
    ],
)
def test_invalid_telegram_settings_fail_before_the_host_changes(
    host: Host, token: str, chat_id: str, message: str
) -> None:
    result = host.deploy(ALERT_TELEGRAM_BOT_TOKEN=token, ALERT_TELEGRAM_CHAT_ID=chat_id)

    assert result.returncode == 1
    assert message in result.stderr
    assert not host.env_file.exists()
    assert host.log("psql.log") == ""
    assert " up " not in host.log("docker.log")
    if token:
        assert token not in result.stdout + result.stderr


BUDGET_SILENT = "LLM_MONTHLY_BUDGET_USD is not set; budget alerts are silent"
# Without a locale where a range like [0-9] matches other digits too, such values prove nothing.
OTHER_DIGITS = pytest.mark.skipif(
    subprocess.run(
        ["bash", "-c", "[[ ５ =~ ^[0-9]$ ]]"],
        env={"PATH": os.environ["PATH"], "LC_ALL": "en_US.UTF-8"},
        capture_output=True,
        check=False,
    ).returncode
    != 0,
    reason="no en_US.UTF-8 locale where [0-9] matches other digits",
)


def _budget_file(host: Host) -> Path:
    return host.monitoring_dir / "textfile" / "llm_budget.prom"


def test_monthly_llm_budget_reaches_node_exporter_kept_by_a_manual_run_dropped_when_empty(
    host: Host,
) -> None:
    fresh = host.deploy(unset=("LLM_MONTHLY_BUDGET_USD",))
    fresh_file = _budget_file(host).exists()
    configured = host.deploy(LLM_MONTHLY_BUDGET_USD="250.50")
    budget = _budget_file(host)
    written = _read(budget)
    mode = stat.S_IMODE(budget.stat().st_mode)
    textfiles = sorted(path.name for path in budget.parent.iterdir())
    kept = host.deploy(unset=("LLM_MONTHLY_BUDGET_USD",))
    kept_content = _read(budget)
    dropped = host.deploy(LLM_MONTHLY_BUDGET_USD="")

    assert fresh.returncode == 0, fresh.stderr
    assert not fresh_file
    assert BUDGET_SILENT in fresh.stderr
    assert configured.returncode == 0, configured.stderr
    assert written == (
        "# HELP dmc_llm_monthly_budget_usd Monthly LLM budget in US dollars"
        " (LLM_MONTHLY_BUDGET_USD of the monitoring deploy).\n"
        "# TYPE dmc_llm_monthly_budget_usd gauge\n"
        "dmc_llm_monthly_budget_usd 250.50\n"
    )
    # node-exporter (nobody) reads it; the collector never sees a half-written *.prom.
    assert mode == 0o644
    assert textfiles == ["llm_budget.prom"]
    assert list(host.monitoring_dir.glob(".llm_budget.*")) == []
    assert BUDGET_SILENT not in configured.stderr
    assert "budget: monthly LLM budget of 250.50 USD" in configured.stdout
    assert kept.returncode == 0, kept.stderr
    assert kept_content == written
    assert BUDGET_SILENT not in kept.stderr
    # Without the file the budget alerts stay silent instead of firing on an old threshold.
    assert dropped.returncode == 0, dropped.stderr
    assert not budget.exists()
    assert BUDGET_SILENT in dropped.stderr


@pytest.mark.parametrize("budget", ["0", "0.00"])
def test_zero_budget_means_no_budget(host: Host, budget: str) -> None:
    assert host.deploy(LLM_MONTHLY_BUDGET_USD="250.50").returncode == 0
    result = host.deploy(LLM_MONTHLY_BUDGET_USD=budget)

    assert result.returncode == 0, result.stderr
    assert not _budget_file(host).exists()
    assert BUDGET_SILENT in result.stderr


def test_budget_file_is_written_through_a_root_owned_dir(host: Host) -> None:
    # pg-backup writes the textfile dir too: a link it planted there must not lead the deploy to a
    # file outside.
    textfile = host.monitoring_dir / "textfile"
    textfile.mkdir()
    outside = host.root / "outside"
    outside.mkdir()
    (outside / "file").write_text("host file\n", encoding="utf-8")
    (textfile / "llm_budget.prom.tmp").symlink_to(outside / "file")
    (textfile / "llm_budget.prom").symlink_to(outside)
    result = host.deploy(LLM_MONTHLY_BUDGET_USD="250.50")

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in outside.iterdir()) == ["file"]
    assert _read(outside / "file") == "host file\n"
    assert not _budget_file(host).is_symlink()
    assert "dmc_llm_monthly_budget_usd 250.50\n" in _read(_budget_file(host))


def test_failed_budget_write_leaves_no_temporary_file(host: Host) -> None:
    (host.monitoring_dir / "textfile" / "llm_budget.prom").mkdir(parents=True)
    result = host.deploy(LLM_MONTHLY_BUDGET_USD="250.50")

    assert result.returncode != 0
    assert list(host.monitoring_dir.glob(".llm_budget.*")) == []
    assert " up " not in host.log("docker.log")


@pytest.mark.parametrize(
    "budget",
    [
        "abc",
        "-5",
        "1e3",
        "50\nx 1",
        "1.234",
        "050",
        " 50",
        pytest.param("５０", marks=OTHER_DIGITS),
        pytest.param("٥٠", marks=OTHER_DIGITS),
    ],
)
def test_invalid_budget_fails_before_the_host_changes(host: Host, budget: str) -> None:
    # A UTF-8 locale, where a range like [0-9] matches other digits too.
    result = host.deploy(LLM_MONTHLY_BUDGET_USD=budget, LC_ALL="en_US.UTF-8")

    assert result.returncode == 1
    assert (
        "LLM_MONTHLY_BUDGET_USD must be a number of US dollars with at most two decimals"
        in result.stderr
    )
    assert not host.env_file.exists()
    assert host.log("psql.log") == ""
    assert " up " not in host.log("docker.log")
    assert not _budget_file(host).exists()


def test_edge_mode_scrapes_caddy_and_needs_a_https_grafana_url(host: Host) -> None:
    result = host.deploy()
    targets = _read(host.monitoring_dir / "prometheus" / "targets" / "caddy.json")
    refused = host.deploy(GRAFANA_ROOT_URL="https://staging-api.example.test/grafana/")

    assert result.returncode == 0, result.stderr
    assert targets == '[{"targets": ["dmc-268-edge-caddy-1:9180"]}]\n'
    assert host.env()["GRAFANA_COOKIE_SECURE"] == "true"
    assert "-f " + str(host.monitoring_dir / "compose.edge.yml") in host.log("docker.log")
    assert refused.returncode == 1
    assert "GRAFANA_ROOT_URL must be https://<host>/ in edge mode" in refused.stderr


def test_ports_mode_keeps_grafana_on_loopback_and_scrapes_no_proxy(host: Host) -> None:
    env_file = host.api_dir / ".env"
    env_file.write_text(_read(env_file).replace("DEPLOY_MODE=edge", "DEPLOY_MODE=ports"))
    result = host.deploy(GRAFANA_ROOT_URL="https://grafana.example.test/")

    assert result.returncode == 0, result.stderr
    assert _read(host.monitoring_dir / "prometheus" / "targets" / "caddy.json") == "[]\n"
    assert host.env()["GRAFANA_ROOT_URL"] == "http://localhost:3000/"
    assert host.env()["GRAFANA_COOKIE_SECURE"] == "false"
    assert "-f " + str(host.monitoring_dir / "compose.ports.yml") in host.log("docker.log")


def test_edge_mode_probes_the_public_staging_names(host: Host) -> None:
    result = host.deploy()
    probes = host.monitoring_dir / "prometheus" / "targets" / "probes.json"
    targets = json.loads(_read(probes))

    assert result.returncode == 0, result.stderr
    # Written under umask 077, read by Prometheus as nobody.
    assert stat.S_IMODE(probes.stat().st_mode) == 0o644
    # The health path of each service behind the edge proxy; the name labels the alerts.
    assert targets == [
        {
            "targets": ["https://staging-api.example.test/healthcheck"],
            "labels": {"host": "staging-api.example.test", "environment": "staging"},
        },
        {
            "targets": ["https://staging-ui.example.test/health"],
            "labels": {"host": "staging-ui.example.test", "environment": "staging"},
        },
    ]


def test_ports_mode_probes_no_public_name_and_needs_no_domain(host: Host) -> None:
    assert host.deploy().returncode == 0
    env_file = host.api_dir / ".env"
    env_file.write_text(_read(env_file).replace("DEPLOY_MODE=edge", "DEPLOY_MODE=ports"))
    result = host.deploy(unset=("APP_DOMAIN",))

    assert result.returncode == 0, result.stderr
    assert _read(host.monitoring_dir / "prometheus" / "targets" / "probes.json") == "[]\n"


@pytest.mark.parametrize(
    "domain", [None, "", "example", "Example.test", "example.test/x", 'example.test"']
)
def test_edge_mode_without_a_valid_domain_fails_before_the_host_changes(
    host: Host, domain: str | None
) -> None:
    if domain is None:
        result = host.deploy(unset=("APP_DOMAIN",))
    else:
        result = host.deploy(APP_DOMAIN=domain)

    assert result.returncode == 1
    assert "APP_DOMAIN must be the base domain of the public names in edge mode" in result.stderr
    assert not host.env_file.exists()
    assert host.log("psql.log") == ""
    assert " up " not in host.log("docker.log")
    assert not (host.monitoring_dir / "prometheus" / "targets" / "probes.json").exists()


def test_failed_deploy_keeps_the_probe_targets_of_the_last_one(host: Host) -> None:
    # The upload step extracts the stack over the host's dir without removing it, like cp; with no
    # probes.json in the repository, a deploy that fails before writing the targets keeps the
    # previous ones instead of leaving the public names unprobed and silent.
    assert host.deploy().returncode == 0
    subprocess.run(["cp", "-R", f"{MONITORING}/.", str(host.monitoring_dir)], check=True)
    result = host.deploy(APP_DOMAIN="")
    targets = json.loads(_read(host.monitoring_dir / "prometheus" / "targets" / "probes.json"))

    assert result.returncode == 1
    assert [group["labels"]["host"] for group in targets] == [
        "staging-api.example.test",
        "staging-ui.example.test",
    ]
    assert not re.search(r"^\s+rm:", _job("deploy-monitoring"), re.M)


def _probe(name: str, path: str, environment: str) -> dict[str, object]:
    probed = f"{name}.example.test"
    return {
        "targets": [f"https://{probed}/{path}"],
        "labels": {"host": probed, "environment": environment},
    }


STAGING_PROBES = [
    _probe("staging-api", "healthcheck", "staging"),
    _probe("staging-ui", "health", "staging"),
]
PROD_PROBES = [_probe("api", "healthcheck", "prod"), _probe("ui", "health", "prod")]


def _probe_targets(host: Host) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = json.loads(
        _read(host.monitoring_dir / "prometheus" / "targets" / "probes.json")
    )
    return targets


@pytest.mark.parametrize(
    ("names", "prod_probes"),
    [
        ("", []),
        ("api", PROD_PROBES[:1]),
        ("api ui", PROD_PROBES),
        (" ui  api ui ", PROD_PROBES),
    ],
)
def test_production_names_join_the_probes_by_a_variable(
    host: Host, names: str, prod_probes: list[dict[str, object]]
) -> None:
    result = host.deploy(PROBE_PROD_HOSTS=names)

    assert result.returncode == 0, result.stderr
    # Each production name with the health path of its service, after the staging ones.
    assert _probe_targets(host) == STAGING_PROBES + prod_probes


def test_manual_run_keeps_and_an_empty_value_drops_the_production_names(host: Host) -> None:
    assert host.deploy(PROBE_PROD_HOSTS="api ui").returncode == 0
    stored = host.env()["PROBE_PROD_HOSTS"]
    kept = host.deploy(unset=("PROBE_PROD_HOSTS",))
    kept_targets = _probe_targets(host)
    dropped = host.deploy(PROBE_PROD_HOSTS="")

    assert stored == "api ui"
    assert kept.returncode == 0, kept.stderr
    assert kept_targets == STAGING_PROBES + PROD_PROBES
    assert dropped.returncode == 0, dropped.stderr
    assert _probe_targets(host) == STAGING_PROBES
    assert host.env()["PROBE_PROD_HOSTS"] == ""


@pytest.mark.parametrize("names", ["web", "API", "staging-api", "api,ui", "apiui", 'api"'])
def test_unknown_production_names_fail_before_the_host_changes(host: Host, names: str) -> None:
    result = host.deploy(PROBE_PROD_HOSTS=names)

    assert result.returncode == 1
    assert "PROBE_PROD_HOSTS must list production names out of: api ui" in result.stderr
    assert not host.env_file.exists()
    assert host.log("psql.log") == ""
    assert " up " not in host.log("docker.log")
    assert not (host.monitoring_dir / "prometheus" / "targets" / "probes.json").exists()


def test_invalid_production_names_kept_in_env_fail_before_the_host_changes(host: Host) -> None:
    host.env_file.write_text("PROBE_PROD_HOSTS=web\n", encoding="utf-8")
    result = host.deploy(unset=("PROBE_PROD_HOSTS",))

    assert result.returncode == 1
    assert "PROBE_PROD_HOSTS must list production names out of: api ui" in result.stderr
    assert _read(host.env_file) == "PROBE_PROD_HOSTS=web\n"
    assert host.log("psql.log") == ""
    assert " up " not in host.log("docker.log")


def test_ports_mode_probes_no_production_name_either(host: Host) -> None:
    env_file = host.api_dir / ".env"
    env_file.write_text(_read(env_file).replace("DEPLOY_MODE=edge", "DEPLOY_MODE=ports"))
    result = host.deploy(PROBE_PROD_HOSTS="api ui")

    assert result.returncode == 0, result.stderr
    assert _read(host.monitoring_dir / "prometheus" / "targets" / "probes.json") == "[]\n"


def test_deploy_refuses_without_the_application_stack(host: Host) -> None:
    (host.api_dir / ".env").unlink()
    result = host.deploy()

    assert result.returncode == 1
    assert "deploy the application stack first" in result.stderr
    assert host.log("docker.log") == ""


def test_failed_stack_start_fails_the_deploy(host: Host) -> None:
    result = host.deploy(STUB_UP_FAILS="1")

    assert result.returncode != 0
    assert host.log("grafana.log") == ""


def _render_alertmanager(tmp_path: Path, chat_id: str) -> str:
    config = tmp_path / "alertmanager.yml"
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; render_alertmanager_config "$2" "$3"',
            "render",
            str(MONITORING / "alertmanager-config.sh"),
            str(config),
            chat_id,
        ],
        check=True,
    )
    return _read(config)


def test_event_alerts_are_sent_once_and_never_as_resolved(tmp_path: Path) -> None:
    config = _render_alertmanager(tmp_path, CHAT_ID)
    rules = _read(MONITORING / "prometheus" / "rules" / "alerts.yml")
    events = re.findall(r"- alert: (\w+)\n(?:        .*\n)*?          kind: event\n", rules)

    assert events == ["ReviewRunsFailed", "ReviewDeadLetters", "ReviewRunsBudgetPaused"]
    assert (
        '  routes:\n    - matchers: [kind="event"]\n      receiver: telegram-events\n'
        "      group_by: [environment]\n      group_wait: 90s\n"
    ) in config
    receivers = config[config.index("receivers:") :]
    assert re.findall(r"- name: (\S+)\n(?:.*\n)*?        send_resolved: (\w+)", receivers) == [
        ("telegram", "true"),
        ("telegram-events", "false"),
    ]


def test_without_telegram_both_routes_deliver_nothing(tmp_path: Path) -> None:
    config = _render_alertmanager(tmp_path, "")

    assert "  receiver: none\n" in config
    assert "      receiver: none\n" in config
    assert "telegram_configs" not in config


def test_backup_role_reads_everything_but_cannot_administer(host: Host) -> None:
    result = host.deploy()
    sql = host.log("psql.log")

    assert result.returncode == 0, result.stderr
    assert "GRANT pg_read_all_data TO backup_agent;" in sql
    assert (
        "ALTER ROLE backup_agent WITH LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION" in sql
    )
    assert "pg_write_all_data" not in sql
    # The column grants of the metadata roles are rebuilt without touching the backup role.
    assert "FROM grafana_reader, metrics_exporter;" in sql
    assert "backup_agent;" not in sql.split("REVOKE", 1)[1].split("\n", 1)[0]


def test_column_grants_reach_postgres_after_they_are_revoked(host: Host) -> None:
    result = host.deploy()
    sql = host.log("psql.log")
    revoke = "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_reader, metrics_exporter;"
    exporter_cost = "GRANT SELECT (cost_usd, created_at) ON usage_events TO metrics_exporter;"

    assert result.returncode == 0, result.stderr
    assert _grants(sql) == METADATA_GRANTS
    # A grant sent before the REVOKE would be dropped by it.
    assert sql.index(revoke) < sql.index("GRANT SELECT (") < sql.index(exporter_cost)


def test_backup_dirs_belong_to_the_backup_user(host: Host) -> None:
    result = host.deploy()
    backup_dir = host.root / "backups"
    textfile = host.monitoring_dir / "textfile"

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(textfile.stat().st_mode) == 0o755
    assert f"70:70 {backup_dir}" in host.log("chown.log")
    assert f"70:70 {textfile}" in host.log("chown.log")
    assert host.env()["BACKUP_DIR"] == str(backup_dir)


def test_probe_config_is_readable_by_the_exporter_user(host: Host) -> None:
    config_dir = host.monitoring_dir / "blackbox"
    config_dir.chmod(0o700)
    (config_dir / "blackbox.yml").chmod(0o600)
    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((config_dir / "blackbox.yml").stat().st_mode) == 0o644


def test_status_page_files_are_readable_by_its_user(host: Host) -> None:
    config_dir = host.monitoring_dir / "status"
    config_dir.chmod(0o700)
    (config_dir / "status_page.py").chmod(0o600)
    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((config_dir / "status_page.py").stat().st_mode) == 0o644
    assert re.fullmatch(r"[0-9a-f]{16}", host.env()["STATUS_CONFIG_DIGEST"])


# --- pg-backup.sh with fake PostgreSQL tools -----------------------------------------------

SOURCE_FINGERPRINT = "alembic_version=20261015_0032\nrun_actions=10\nruns=3"

FAKE_PG_TOOLS = {
    # Writes the dump unless told to fail; logs every call.
    "pg_dump": """\
#!/bin/sh
echo "pg_dump $*" >> "${STUB_PG_LOG}"
[ -n "${STUB_DUMP_FAILS:-}" ] && { echo "pg_dump: connection refused" >&2; exit 1; }
for arg; do case "$arg" in --file=*) echo dump > "${arg#--file=}" ;; esac; done
""",
    "pg_restore": """\
#!/bin/sh
echo "pg_restore $*" >> "${STUB_PG_LOG}"
""",
    # -c: the database size, or the fingerprint of the restored database (-d). Without -c: the
    # snapshot session fed through the fifo — it answers the snapshot id, the fingerprint of the
    # source and \echo lines, and logs every statement it reads until the fifo closes.
    "psql": r"""\
#!/bin/sh
printf 'psql %s\n' "$(printf '%s' "$*" | tr '\n' ' ')" >> "${STUB_PG_LOG}"
case "$*" in
  *pg_database_size*) echo "${STUB_DB_BYTES:-1024}"; exit 0 ;;
  *"-d "*) printf '%s\n' "${STUB_RESTORED_FINGERPRINT}"; exit 0 ;;
esac
while IFS= read -r line; do
  echo "session: ${line}" >> "${STUB_PG_LOG}"
  case "${line}" in
    *pg_export_snapshot*) echo "00000003-0000001B-1" ;;
    *"ORDER BY ord, line;"*) printf '%s\n' "${STUB_SOURCE_FINGERPRINT}" ;;
    '\echo '*) echo "${line#\\echo }" ;;
  esac
done
""",
    "createdb": """\
#!/bin/sh
echo "createdb $*" >> "${STUB_PG_LOG}"
""",
    "dropdb": """\
#!/bin/sh
echo "dropdb $*" >> "${STUB_PG_LOG}"
""",
    # GNU/busybox `stat -c %Y FILE` on any platform.
    "stat": """\
#!/bin/sh
python3 -c 'import os, sys; print(int(os.path.getmtime(sys.argv[1])))' "$3"
""",
}


@dataclass
class BackupRun:
    result: subprocess.CompletedProcess[str]
    backups: Path
    metrics: dict[str, str]
    calls: list[str]


def _run_backup(tmp_path: Path, **env: str) -> BackupRun:
    tools = tmp_path / "pgbin"
    tools.mkdir(exist_ok=True)
    for name, source in FAKE_PG_TOOLS.items():
        (tools / name).write_text(source, encoding="utf-8")
        (tools / name).chmod(0o755)
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    textfile = tmp_path / "pg_backup.prom"
    log = tmp_path / "pg.log"
    # /bin/sh, not PATH: a local sh wrapper first on PATH would run instead of the system shell.
    result = subprocess.run(
        ["/bin/sh", str(MONITORING / "backup" / "pg-backup.sh"), "once"],
        env={
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PGDATABASE": "app",
            "BACKUP_DIR": str(backups),
            "TEXTFILE": str(textfile),
            "BACKUP_SNAPSHOT_WAIT_SECONDS": "10",
            "STUB_PG_LOG": str(log),
            "STUB_SOURCE_FINGERPRINT": SOURCE_FINGERPRINT,
            "STUB_RESTORED_FINGERPRINT": SOURCE_FINGERPRINT,
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    metrics = {}
    if textfile.exists():
        for line in _read(textfile).splitlines():
            if not line.startswith("#"):
                name, _, value = line.partition(" ")
                metrics[name] = value
    calls = _read(log).splitlines() if log.exists() else []
    return BackupRun(result, backups, metrics, calls)


def _commands(run: BackupRun) -> list[str]:
    return [call.split()[0] for call in run.calls if not call.startswith("session:")]


def test_backup_keeps_a_dump_only_after_the_restore_check(tmp_path: Path) -> None:
    run = _run_backup(tmp_path)
    dumps = sorted(path.name for path in run.backups.iterdir())
    pg_dump = next(call for call in run.calls if call.startswith("pg_dump"))

    assert run.result.returncode == 0, run.result.stderr
    assert len(dumps) == 1 and re.fullmatch(r"app-\d{8}T\d{6}Z\.dump", dumps[0])
    assert stat.S_IMODE((run.backups / dumps[0]).stat().st_mode) == 0o600
    # Disk check, snapshot session, the dump of exactly that snapshot, then the scratch restore.
    assert _commands(run) == [
        "psql",
        "psql",
        "pg_dump",
        "dropdb",
        "createdb",
        "pg_restore",
        "psql",
        "dropdb",
    ]
    assert "--snapshot=00000003-0000001B-1" in pg_dump
    assert "--no-owner" not in pg_dump  # the dump keeps owners and grants for a real restore
    assert "session: BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;" in run.calls
    assert run.calls[-1] == "dropdb dmc_restore_check"
    # The transaction ends only after the dump.
    assert run.calls.index("session: COMMIT;") > run.calls.index(pg_dump)
    assert "revision" not in run.result.stdout
    assert "alembic_version=20261015_0032, 2 tables, 13 rows match" in run.result.stdout
    assert run.metrics["dmc_pg_backup_last_run_success"] == "1"
    assert run.metrics["dmc_pg_backup_files"].strip() == "1"
    assert "dmc_pg_backup_last_success_timestamp_seconds" in run.metrics


def test_failed_dump_never_reaches_the_restore_check(tmp_path: Path) -> None:
    run = _run_backup(tmp_path, STUB_DUMP_FAILS="1")

    assert run.result.returncode == 1
    assert not [call for call in run.calls if call.startswith(("pg_restore", "createdb"))]
    assert list(run.backups.iterdir()) == []
    assert run.metrics["dmc_pg_backup_last_run_success"] == "0"
    # Without any verified dump there is no success timestamp: PostgresBackupStale cannot fire
    # on a fake age, PostgresBackupFailed does.
    assert "dmc_pg_backup_last_success_timestamp_seconds" not in run.metrics


@pytest.mark.parametrize(
    "restored",
    [
        "alembic_version=20261001_0020\nrun_actions=10\nruns=3",  # another revision
        "alembic_version=20261015_0032\nrun_actions=9\nruns=3",  # a lost row
        "alembic_version=20261015_0032\nruns=3",  # a lost table
    ],
)
def test_dump_that_does_not_reproduce_its_snapshot_is_discarded(
    tmp_path: Path, restored: str
) -> None:
    run = _run_backup(tmp_path, STUB_RESTORED_FINGERPRINT=restored)

    assert run.result.returncode == 1
    assert list(run.backups.iterdir()) == []
    assert "restore check failed: the restored database differs" in run.result.stdout
    assert run.calls[-1] == "dropdb --if-exists dmc_restore_check"
    assert run.metrics["dmc_pg_backup_last_run_success"] == "0"


def test_backup_refuses_without_disk_for_the_dump_and_the_restore(tmp_path: Path) -> None:
    run = _run_backup(tmp_path, STUB_DB_BYTES=str(10**18))

    assert run.result.returncode == 1
    assert "not enough free disk" in run.result.stdout
    assert _commands(run) == ["psql", "dropdb"]
    assert list(run.backups.iterdir()) == []


# --- off-host copy of the dumps ------------------------------------------------------------

PASSPHRASE = "test-passphrase-with-enough-entropy-0123456789"
OFFSITE = {
    "BACKUP_S3_ENDPOINT": "https://0123456789abcdef.r2.cloudflarestorage.com",
    "BACKUP_S3_BUCKET": "dmc268-t6-backups",
    "BACKUP_S3_ACCESS_KEY_ID": "FAKE_TEST_KEY_ID_DO_NOT_USE",
    "BACKUP_S3_SECRET_ACCESS_KEY": "FAKE_TEST_CREDENTIAL_DO_NOT_USE",
    "BACKUP_ENCRYPTION_PASSPHRASE": PASSPHRASE,
}
NO_OFFSITE = dict.fromkeys(OFFSITE, "")


def test_offsite_names_reach_only_the_monitoring_deploy() -> None:
    job = _job("deploy-monitoring")

    for name in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET"):
        assert f"{name}: ${{{{ vars.{name} }}}}" in job
    for name in (
        "BACKUP_S3_ACCESS_KEY_ID",
        "BACKUP_S3_SECRET_ACCESS_KEY",
        "BACKUP_ENCRYPTION_PASSPHRASE",
    ):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in job
        assert name not in _job("deploy-staging")
    envs = re.search(r"^          envs: (\S+)$", job, re.M)
    assert envs is not None
    assert set(OFFSITE) <= set(envs.group(1).split(","))


def test_offsite_file_names_stay_plain_across_passphrase_changes() -> None:
    compose = _read(MONITORING / "compose.offsite.yml")

    # With encrypted names a new passphrase could not list the older copies, so the 30-day prune
    # would never remove them (docs/MONITORING.md §9, "Смена ключа").
    assert re.search(r'^\s+RCLONE_CONFIG_SECURE_FILENAME_ENCRYPTION: "off"$', compose, re.M)
    assert "--min-age" in _read(MONITORING / "backup" / "offsite.sh")


def test_offsite_copy_is_encrypted_and_the_passphrase_never_stored(host: Host) -> None:
    result = host.deploy(extra=OFFSITE)
    env = host.env()

    assert result.returncode == 0, result.stderr
    assert "-f " + str(host.monitoring_dir / "compose.offsite.yml") in host.log("docker.log")
    # The passphrase reaches rclone obscure through stdin only, then only its obscured form and
    # its digest stay on the host.
    assert host.log("obscure.log") == PASSPHRASE
    assert env["BACKUP_ENCRYPTION_PASSWORD_OBSCURED"].startswith("obscured_")
    assert len(env["BACKUP_ENCRYPTION_PASSPHRASE_SHA256"]) == 64
    assert PASSPHRASE not in _read(host.env_file)
    for secret in (PASSPHRASE, OFFSITE["BACKUP_S3_SECRET_ACCESS_KEY"]):
        assert secret not in host.log("docker.log")
        assert secret not in result.stdout + result.stderr
    assert env["BACKUP_S3_PROVIDER"] == "Cloudflare"
    assert "backups: encrypted off-host copy to bucket dmc268-t6-backups" in result.stdout


def test_same_passphrase_keeps_its_obscured_form(host: Host) -> None:
    assert host.deploy(extra=OFFSITE).returncode == 0
    first = host.env()["BACKUP_ENCRYPTION_PASSWORD_OBSCURED"]
    assert host.deploy(extra=OFFSITE).returncode == 0
    second = host.env()["BACKUP_ENCRYPTION_PASSWORD_OBSCURED"]
    rotated = host.deploy(
        extra={**OFFSITE, "BACKUP_ENCRYPTION_PASSPHRASE": PASSPHRASE + "-rotated"}
    )

    # A new obscured value would recreate the service on every deploy.
    assert second == first
    assert rotated.returncode == 0
    assert host.env()["BACKUP_ENCRYPTION_PASSWORD_OBSCURED"] != first


def test_manual_run_keeps_and_empty_values_disable_the_offsite_copy(host: Host) -> None:
    assert host.deploy(extra=OFFSITE).returncode == 0
    kept = host.deploy(unset=tuple(OFFSITE))
    kept_env = host.env()
    disabled = host.deploy(extra=NO_OFFSITE)

    assert kept.returncode == 0, kept.stderr
    assert kept_env["BACKUP_S3_BUCKET"] == "dmc268-t6-backups"
    assert kept.stdout.count("compose.offsite.yml") == 0  # nothing printed, the file is passed
    assert disabled.returncode == 0, disabled.stderr
    assert not any(key.startswith("BACKUP_S3") for key in host.env())
    assert "dumps stay on this host only" in disabled.stderr
    last_up = [line for line in host.log("docker.log").splitlines() if " up " in line][-1]
    assert "compose.offsite.yml" not in last_up


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"BACKUP_S3_BUCKET": ""}, "set all of BACKUP_S3_ENDPOINT"),
        ({"BACKUP_S3_ENDPOINT": "http://r2.example.test"}, "BACKUP_S3_ENDPOINT must be https://"),
        ({"BACKUP_S3_BUCKET": "Bad_Bucket"}, "BACKUP_S3_BUCKET is not a valid bucket name"),
        (
            {"BACKUP_S3_ACCESS_KEY_ID": "key with spaces"},
            "BACKUP_S3_ACCESS_KEY_ID has an unexpected",
        ),
        ({"BACKUP_ENCRYPTION_PASSPHRASE": "short"}, "at least 32 characters"),
    ],
)
def test_invalid_offsite_settings_fail_before_the_host_changes(
    host: Host, override: dict[str, str], message: str
) -> None:
    result = host.deploy(extra={**OFFSITE, **override})

    assert result.returncode == 1
    assert message in result.stderr
    assert not host.env_file.exists()
    assert host.log("psql.log") == ""
    assert PASSPHRASE not in result.stdout + result.stderr


FAKE_RCLONE = """\
#!/bin/sh
echo "rclone $*" >> "${STUB_RCLONE_LOG}"
if [ "$1" = copy ] && [ -n "${STUB_COPY_FAILS:-}" ]; then
  echo "Failed to copy: 403 Forbidden" >&2
  exit 1
fi
"""


def _run_offsite(
    tmp_path: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    tools = tmp_path / "rclonebin"
    tools.mkdir(exist_ok=True)
    (tools / "rclone").write_text(FAKE_RCLONE, encoding="utf-8")
    (tools / "rclone").chmod(0o755)
    textfile = tmp_path / "pg_backup_offsite.prom"
    log = tmp_path / "rclone.log"
    # /bin/sh, not PATH: a local sh wrapper first on PATH would run instead of the system shell.
    result = subprocess.run(
        ["/bin/sh", str(MONITORING / "backup" / "offsite.sh"), "once"],
        env={
            "PATH": f"{tools}:{os.environ['PATH']}",
            "BACKUP_DIR": str(tmp_path),
            "TEXTFILE": str(textfile),
            "STUB_RCLONE_LOG": str(log),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls = _read(log).splitlines() if log.exists() else []
    return result, calls, _read(textfile) if textfile.exists() else ""


def test_offsite_copies_finished_dumps_then_prunes_old_copies(tmp_path: Path) -> None:
    result, calls, metrics = _run_offsite(tmp_path)

    assert result.returncode == 0, result.stderr
    assert calls == [
        f"rclone copy {tmp_path} secure: --include *.dump --immutable --no-traverse",
        "rclone delete secure: --include *.dump --min-age 30d",
    ]
    assert "dmc_pg_backup_offsite_last_run_success 1" in metrics
    assert "dmc_pg_backup_offsite_last_success_timestamp_seconds " in metrics


def test_failed_offsite_copy_never_deletes_remote_dumps(tmp_path: Path) -> None:
    result, calls, metrics = _run_offsite(tmp_path, STUB_COPY_FAILS="1")

    assert result.returncode == 1
    assert [call.split()[1] for call in calls] == ["copy"]
    assert "dmc_pg_backup_offsite_last_run_success 0" in metrics
    assert "last_success_timestamp" not in metrics
