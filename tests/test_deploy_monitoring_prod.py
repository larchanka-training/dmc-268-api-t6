"""Static contracts of the prod side of the monitoring stack (docs/MONITORING.md).

No Docker daemon: Prometheus rules are run by promtool in scripts/validate_monitoring.sh; the tests
here read the YAML files of deploy/monitoring and tests/monitoring.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "deploy" / "monitoring"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_every_alert_has_a_case_that_checks_its_environment() -> None:
    rules = _load(MONITORING / "prometheus" / "rules" / "alerts.yml")
    alerts = {
        rule["alert"] for group in rules["groups"] for rule in group["rules"] if "alert" in rule
    }
    cases = _load(ROOT / "tests" / "monitoring" / "alerts.test.yml")
    with_environment = {
        check["alertname"]
        for case in cases["tests"]
        for check in case.get("alert_rule_test", [])
        for expected in check.get("exp_alerts") or []
        if "environment" in expected.get("exp_labels", {})
    }
    assert alerts - with_environment == set()


PROMETHEUS_YML = MONITORING / "prometheus" / "prometheus.yml"
RELABEL_FIELDS = {"source_labels", "regex", "target_label", "replacement", "action"}


def _jobs() -> dict[str, dict[str, Any]]:
    config = _load(PROMETHEUS_YML)
    return {job["job_name"]: job for job in config["scrape_configs"]}


def _relabel(rules: list[dict[str, Any]], labels: dict[str, str]) -> dict[str, str] | None:
    """Prometheus relabelling for the two actions the jobs use; None when a series is dropped.

    The regex is anchored on both ends and the source labels are joined with ';', as in Prometheus.
    """
    result = dict(labels)
    for rule in rules:
        action = rule.get("action", "replace")
        value = ";".join(result.get(name, "") for name in rule.get("source_labels", []))
        matched = re.fullmatch(rule.get("regex", "(.*)"), value)
        if action == "drop":
            if matched:
                return None
        elif matched:
            replacement = rule.get("replacement", "$1")
            result[rule["target_label"]] = matched.expand(re.sub(r"\$(\d+)", r"\\\1", replacement))
    return result


def test_prometheus_sets_no_global_environment() -> None:
    # A global label would stamp every series, prod included, with one environment.
    assert "external_labels" not in _load(PROMETHEUS_YML)["global"]


def test_every_scrape_target_names_its_environment() -> None:
    jobs = _jobs()
    staging = ("postgres", "redis", "rabbitmq", "rabbitmq-queues")
    shared = ("prometheus", "alertmanager", "grafana", "node", "cadvisor")

    for name in staging + shared:
        static = jobs[name]["static_configs"]
        assert len(static) == 1, name
        assert static[0]["labels"] == {"environment": "staging" if name in staging else "shared"}
    # The edge proxy is one for the host; its targets come from a file written by deploy.sh.
    assert _relabel(jobs["caddy"]["relabel_configs"], {"__address__": "caddy:2019"}) == {
        "__address__": "caddy:2019",
        "environment": "shared",
    }


def test_store_jobs_read_the_prod_targets_written_by_deploy() -> None:
    jobs = _jobs()
    expected = {
        "postgres": "postgres-prod.json",
        "redis": "redis-prod.json",
        "rabbitmq": "rabbitmq-prod.json",
        "rabbitmq-queues": "rabbitmq-prod.json",
    }

    for name, file in expected.items():
        assert jobs[name]["file_sd_configs"] == [{"files": [f"/etc/prometheus/targets/{file}"]}]


def test_relabelling_uses_only_the_fields_the_test_evaluates() -> None:
    for name, job in _jobs().items():
        for key in ("relabel_configs", "metric_relabel_configs"):
            for rule in job.get(key, []):
                assert set(rule) <= RELABEL_FIELDS, (name, key, rule)
                assert rule.get("action", "replace") in {"replace", "drop"}, (name, key, rule)


def _series_environment(job: str, series: dict[str, str]) -> str | None:
    config = _jobs()[job]
    target: dict[str, str] = {}
    if "static_configs" in config:
        target.update(config["static_configs"][0].get("labels", {}))
    target = _relabel(config.get("relabel_configs", []), target) or {}
    stored = _relabel(config.get("metric_relabel_configs", []), {**target, **series})
    assert stored is not None
    return stored.get("environment")


@pytest.mark.parametrize(
    ("project", "environment"),
    [
        ("dmc-268-api-staging", "staging"),
        ("dmc-268-api", "staging"),
        ("dmc-268-ui", "staging"),
        ("dmc-268-ui-staging", "staging"),
        ("dmc-268-api-prod", "prod"),
        ("dmc-268-ui-prod", "prod"),
        ("dmc-268-monitoring", "shared"),
        ("dmc-268-edge", "shared"),
    ],
)
def test_container_series_get_the_environment_of_their_project(
    project: str, environment: str
) -> None:
    series = {"container_label_com_docker_compose_project": project}

    assert _series_environment("cadvisor", series) == environment


@pytest.mark.parametrize(
    ("host", "environment"),
    [
        ("staging-api.x", "staging"),
        ("staging-ui.x", "staging"),
        ("staging-webhook.x", "staging"),
        ("staging.x", "staging"),
        ("staging-www.x", "staging"),
        ("api.x", "prod"),
        ("ui.x", "prod"),
        ("webhook.x", "prod"),
        ("grafana.x", "shared"),
        ("x", "shared"),
        ("www.x", "shared"),
        ("_other", "shared"),
    ],
)
def test_request_series_get_the_environment_of_their_host(host: str, environment: str) -> None:
    assert _series_environment("caddy", {"host": host}) == environment


@pytest.mark.parametrize(
    ("metric", "environment"),
    [
        ("dmc_pg_backup_last_success_timestamp_seconds", "staging"),
        ("dmc_llm_monthly_budget_usd", "shared"),
        ("node_cpu_seconds_total", "shared"),
    ],
)
def test_node_series_get_their_environment(metric: str, environment: str) -> None:
    assert _series_environment("node", {"__name__": metric}) == environment
