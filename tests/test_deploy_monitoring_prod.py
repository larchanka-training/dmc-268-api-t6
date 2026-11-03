"""Static contracts of the prod side of the monitoring stack (docs/MONITORING.md).

No Docker daemon: Prometheus rules are run by promtool in scripts/validate_monitoring.sh; the tests
here read the YAML files of deploy/monitoring and tests/monitoring.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from tests.test_deploy_monitoring import METADATA_GRANTS, Host, _grants, host  # noqa: F401

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
    ("name", "environment"),
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
def test_request_series_get_the_environment_of_their_host(name: str, environment: str) -> None:
    assert _series_environment("caddy", {"host": name}) == environment


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


# --- prod scrapers and the relay (compose.prod.yml) -----------------------------------------

PROD_SCRAPERS = {"postgres-exporter-prod", "redis-exporter-prod", "relay-prod"}


def _merged_services() -> dict[str, dict[str, Any]]:
    """compose.yml, compose.edge.yml and compose.prod.yml merged by service, networks as maps."""
    merged: dict[str, dict[str, Any]] = {}
    for name in ("compose.yml", "compose.edge.yml", "compose.prod.yml"):
        for service, config in _load(MONITORING / name)["services"].items():
            known = merged.setdefault(service, {"networks": {}})
            known.update({key: value for key, value in config.items() if key != "networks"})
            networks = config.get("networks", {})
            if isinstance(networks, list):
                networks = dict.fromkeys(networks, {})
            known["networks"].update(networks)
    return merged


def _members(network: str) -> set[str]:
    return {name for name, config in _merged_services().items() if network in config["networks"]}


def test_prod_scrapers_sit_on_the_prod_network_and_never_on_staging() -> None:
    prod = _load(MONITORING / "compose.prod.yml")

    assert _members("api-prod") == PROD_SCRAPERS
    # check-isolation.sh fails a container on both application networks.
    assert not PROD_SCRAPERS & _members("api")
    assert not _members("api") & _members("api-prod")
    assert prod["networks"]["api-prod"] == {
        "name": "${PROD_API_NETWORK:?PROD_API_NETWORK is required}",
        "external": True,
    }


def test_grafana_shares_no_network_with_the_exporters() -> None:
    # The exporters answer multi-target requests (/probe, /scrape): whoever reaches them can make
    # them connect to any host of the prod network. Grafana lets its admin add data sources, so it
    # reaches the relay only, over a network of its own.
    prod = _load(MONITORING / "compose.prod.yml")
    services = _merged_services()
    exporters = {"postgres-exporter-prod", "redis-exporter-prod"}
    names_and_aliases = {
        alias
        for network in ("prod-metrics", "prod-sql")
        for name in _members(network)
        for alias in [name, *(services[name]["networks"][network] or {}).get("aliases", [])]
    }

    assert prod["networks"]["prod-metrics"] == {"internal": True}
    assert prod["networks"]["prod-sql"] == {"internal": True}
    assert _members("prod-metrics") == {"prometheus", "relay-prod"} | exporters
    assert _members("prod-sql") == {"grafana", "relay-prod"}
    for name in exporters:
        assert not set(services[name]["networks"]) & set(services["grafana"]["networks"]), name
    # The staging names keep resolving through the staging network only.
    assert not names_and_aliases & {"postgres", "rabbitmq", "redis"}


def test_prod_exporters_run_the_images_of_staging() -> None:
    staging = _load(MONITORING / "compose.yml")["services"]
    prod = _load(MONITORING / "compose.prod.yml")["services"]

    assert prod["postgres-exporter-prod"]["image"] == staging["postgres-exporter"]["image"]
    assert prod["redis-exporter-prod"]["image"] == staging["redis-exporter"]["image"]
    assert re.fullmatch(r"nginx:1\.\d+-alpine@sha256:[0-9a-f]{64}", prod["relay-prod"]["image"])
    for name in PROD_SCRAPERS:
        assert re.fullmatch(r"\d+m", str(prod[name]["mem_limit"])), name
        assert "ports" not in prod[name], name


def test_relay_forwards_only_postgres_and_rabbitmq_metrics() -> None:
    config = (MONITORING / "relay" / "nginx.conf").read_text()
    servers = re.findall(r"\bserver\s*\{(.*?)\}", config, re.S)
    forwarded = {}
    for server in servers:
        port = re.search(r"^\s*listen\s+(\d+);", server, re.M)
        target = re.search(r"^\s*set\s+(\$\w+)\s+(\S+);", server, re.M)
        passed = re.search(r"^\s*proxy_pass\s+(\$\w+);", server, re.M)
        assert port and target and passed, server
        # A variable makes nginx resolve the name per connection, so a recreated store is found.
        assert passed.group(1) == target.group(1)
        forwarded[int(port.group(1))] = target.group(2)

    assert forwarded == {15692: "rabbitmq:15692", 5432: "postgres:5432"}
    assert len(servers) == 2
    assert re.search(r"^\s*resolver\s+127\.0\.0\.11\b", config, re.M)


# --- deploy.sh with the prod stack ---------------------------------------------------------

PROD_PROJECT = "dmc-268-api-prod"
PROD_ENV = "POSTGRES_USER=app\nPOSTGRES_DB=app\nREDIS_PASSWORD=prod-pa$$word\nDEPLOY_MODE=edge\n"
ISOLATION_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_ISOLATION_LOG}"
exit "${STUB_ISOLATION_EXIT:-0}"
"""


@pytest.fixture
def prod_host(host: Host) -> Host:  # noqa: F811
    """The host of the `host` fixture with a prod application stack next to the staging one."""
    prod_dir = host.root / "opt" / PROD_PROJECT
    prod_dir.mkdir(parents=True)
    (prod_dir / ".env").write_text(PROD_ENV, encoding="utf-8")
    stub = host.monitoring_dir / "check-isolation.sh"
    stub.write_text(ISOLATION_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return host


def _prod_dir(stack: Host) -> Path:
    return stack.root / "opt" / PROD_PROJECT


def _deploy_with_prod(stack: Host, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PROD_API_DIR": str(_prod_dir(stack)),
        "STUB_ISOLATION_LOG": str(stack.stubs / "isolation.log"),
        **overrides,
    }
    return stack.deploy(extra=env)


def _sql_of(stack: Host, container: str) -> str:
    """What psql received in one container: the log holds a `-- container <id>` line per call."""
    chunks = re.split(r"^-- container (\S+)\n", stack.log("psql.log"), flags=re.M)
    return "".join(
        sql for name, sql in zip(chunks[1::2], chunks[2::2], strict=True) if name == container
    )


def test_prod_roles_get_the_column_grants_of_staging(prod_host: Host) -> None:
    result = _deploy_with_prod(prod_host)
    sql = _sql_of(prod_host, "pgcontainer-prod")
    revoke = "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_reader, metrics_exporter;"

    assert result.returncode == 0, result.stderr
    assert _grants(sql) == METADATA_GRANTS
    # A grant sent before the REVOKE would be dropped by it.
    assert sql.index(revoke) < sql.index("GRANT SELECT (")
    assert sql.count("SET default_transaction_read_only = on;") == 2
    assert "exec -i pgcontainer-prod psql -X -q -v ON_ERROR_STOP=1 -U app -d app" in (
        prod_host.log("docker.log")
    )
    # Staging keeps its own call.
    assert _grants(_sql_of(prod_host, "pgcontainer")) == METADATA_GRANTS


def test_prod_database_gets_no_backup_role(prod_host: Host) -> None:
    result = _deploy_with_prod(prod_host)
    sql = _sql_of(prod_host, "pgcontainer-prod")

    assert result.returncode == 0, result.stderr
    # The backup of prod comes with its own part: no unused high-privilege role in the database.
    assert "backup_agent" not in sql
    assert "pg_read_all_data" not in sql
    assert "PROD_PG_BACKUP_PASSWORD" not in prod_host.env_file.read_text(encoding="utf-8")
    # Staging is unchanged.
    assert "GRANT pg_read_all_data TO backup_agent;" in _sql_of(prod_host, "pgcontainer")


def test_prod_role_passwords_are_their_own_and_go_through_stdin(prod_host: Host) -> None:
    result = _deploy_with_prod(prod_host)
    env = prod_host.env()
    prod_sql = _sql_of(prod_host, "pgcontainer-prod")
    staging_sql = _sql_of(prod_host, "pgcontainer")
    prod_passwords = {
        "grafana_reader": env["PROD_GRAFANA_DB_PASSWORD"],
        "metrics_exporter": env["PROD_PG_EXPORTER_PASSWORD"],
    }

    assert result.returncode == 0, result.stderr
    for role, password in prod_passwords.items():
        assert re.fullmatch(r"[0-9a-f]{48}", password), role
        assert f"ALTER ROLE {role} PASSWORD '{password}';" in prod_sql
        assert password not in staging_sql
        assert password not in prod_host.log("docker.log")  # argv of every docker call
        assert password not in result.stdout + result.stderr
    assert env["PROD_GRAFANA_DB_PASSWORD"] != env["GRAFANA_DB_PASSWORD"]
    assert env["PROD_PG_EXPORTER_PASSWORD"] != env["PG_EXPORTER_PASSWORD"]
    assert env["GRAFANA_DB_PASSWORD"] not in prod_sql
    assert env["PG_EXPORTER_PASSWORD"] not in prod_sql
    assert env["PROD_POSTGRES_DB"] == "app"
    assert env["PROD_API_DIR"] == str(_prod_dir(prod_host))
    assert stat.S_IMODE(prod_host.env_file.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ("no env", "/.env not found: deploy the prod application stack first"),
        ("no postgres", f"expected one running postgres container of project {PROD_PROJECT}"),
        ("no network", f"network {PROD_PROJECT}_default of the prod application stack not found"),
        ("same project", "PROD_API_DIR must be another compose project than API_DIR"),
        ("bad database", "POSTGRES_USER and POSTGRES_DB of the prod stack must be lowercase"),
    ],
)
def test_prod_stack_problems_fail_before_the_host_changes(
    prod_host: Host, problem: str, message: str
) -> None:
    overrides: dict[str, str] = {}
    prod_env = _prod_dir(prod_host) / ".env"
    if problem == "no env":
        prod_env.unlink()
    elif problem == "no postgres":
        overrides["STUB_PROD_POSTGRES_ID"] = ""
    elif problem == "no network":
        overrides["STUB_MISSING_NETWORK"] = f"{PROD_PROJECT}_default"
    elif problem == "same project":
        overrides["PROD_API_DIR"] = str(prod_host.api_dir)
    else:
        prod_env.write_text(PROD_ENV.replace("POSTGRES_DB=app", "POSTGRES_DB=App;x"))
    before = prod_env.read_text() if prod_env.exists() else None

    result = _deploy_with_prod(prod_host, **overrides)

    assert result.returncode == 1
    assert message in result.stderr
    assert " up " not in prod_host.log("docker.log")
    assert not prod_host.env_file.exists()
    assert prod_host.log("psql.log") == ""
    assert (prod_env.read_text() if prod_env.exists() else None) == before


def test_manual_run_keeps_the_prod_dir_and_an_empty_value_drops_it(prod_host: Host) -> None:
    assert _deploy_with_prod(prod_host).returncode == 0
    kept = prod_host.deploy(unset=("PROD_API_DIR",))
    kept_env = prod_host.env()
    kept_sql = _sql_of(prod_host, "pgcontainer-prod")
    dropped = prod_host.deploy(PROD_API_DIR="")

    assert kept.returncode == 0, kept.stderr
    assert kept_env["PROD_API_DIR"] == str(_prod_dir(prod_host))
    assert kept_sql.count("REVOKE ALL ON ALL TABLES") == 2  # both deploys reached prod
    assert dropped.returncode == 0, dropped.stderr
    # Without prod its keys leave .env and the prod database is not touched again.
    assert not {key for key in prod_host.env() if key.startswith("PROD_")}
    assert _sql_of(prod_host, "pgcontainer-prod") == kept_sql


def test_without_prod_nothing_of_it_reaches_the_host(prod_host: Host) -> None:
    # The prod dir exists, but PROD_API_DIR is not given.
    result = prod_host.deploy()

    assert result.returncode == 0, result.stderr
    assert "pgcontainer-prod" not in prod_host.log("psql.log")
    assert not {key for key in prod_host.env() if key.startswith("PROD_")}
