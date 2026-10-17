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

import pytest

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

    assert len(images) == 7
    for image in images:
        assert re.fullmatch(r"[a-z0-9./_-]+:v?[\d.]+@sha256:[0-9a-f]{64}", image), image


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
    # Only what talks to postgres, rabbitmq or redis joins the application network.
    on_api = {name for name, block in blocks.items() if "      api: {}" in block}
    assert on_api == {"prometheus", "grafana", "postgres-exporter", "redis-exporter"}
    # Host collectors never reach the stores or the edge.
    for name in ("node-exporter", "cadvisor"):
        assert "networks:" not in blocks[name]


def test_only_grafana_and_prometheus_join_the_edge_network() -> None:
    edge = _read(MONITORING / "compose.edge.yml")

    assert set(re.findall(r"^  ([a-z-]+):$", edge, re.M)) == {"grafana", "prometheus", "edge"}
    assert "          - ${GRAFANA_ALIAS:?GRAFANA_ALIAS is required}" in edge


def test_config_changes_recreate_the_service_that_reads_them() -> None:
    blocks = _service_blocks(_read(MONITORING / "compose.yml"))
    digests = {
        "prometheus": "PROMETHEUS_CONFIG_DIGEST",
        "alertmanager": "ALERTMANAGER_CONFIG_DIGEST",
        "grafana": "GRAFANA_CONFIG_DIGEST",
        "postgres-exporter": "POSTGRES_EXPORTER_CONFIG_DIGEST",
    }
    deploy = _read(MONITORING / "deploy.sh")

    for service, variable in digests.items():
        assert f"dmc.config-digest: ${{{variable}:-none}}" in blocks[service]
        assert f"printf '{variable}=%s\\n'" in deploy


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
    # The API host is the API only; Prometheus has no authentication and gets no public site.
    assert re.findall(r"reverse_proxy (\S+)", api_site) == ["api-staging:8000"]
    assert not re.search(r"^(prometheus|victoria-metrics)\.", caddyfile, re.M)
    assert "GRAFANA_ROOT_URL: https://grafana.${{ vars.APP_DOMAIN }}/" in _job("deploy-monitoring")


# --- review metrics and roles --------------------------------------------------------------


def test_review_queries_and_grants_never_touch_review_content() -> None:
    queries = _read(MONITORING / "postgres-exporter" / "queries.yml")
    sql = "\n".join(re.findall(r"query: \|\n((?:    .*\n)+)", queries))
    deploy = _read(MONITORING / "deploy.sh")
    grants = re.findall(r"GRANT SELECT \((.*?)\)\s+ON (\w+)", deploy, re.DOTALL)
    dashboard = json.loads(_read(MONITORING / "grafana" / "dashboards" / "overview.json"))
    dashboard_sql = "\n".join(
        target["rawSql"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "rawSql" in target
    )

    assert sql.count("SELECT") == 3
    assert dashboard_sql.count("SELECT") == 9
    assert {table for _, table in grants} == {"runs", "run_actions", "usage_events"}
    granted = {column.strip() for columns, _ in grants for column in columns.split(",")}
    for column in CONTENT_COLUMNS:
        assert not re.search(rf"\b{column}\b", sql), column
        assert column not in granted, column
        assert not re.search(rf"\b{column}\b", dashboard_sql), column
    # Grants are rebuilt from scratch on every deploy and the roles cannot write.
    assert (
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_reader, metrics_exporter;" in deploy
    )
    assert deploy.count("SET default_transaction_read_only = on;") == 2


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
    assert "group: staging-deploy" in job


def test_monitoring_upload_and_secrets_reach_only_the_monitoring_dir() -> None:
    job = _job("deploy-monitoring")

    assert 'source: "deploy/monitoring,deploy/scripts/env-file.sh"' in job
    assert "target: ${{ env.MONITORING_DIR }}" in job
    assert "ALERT_TELEGRAM_BOT_TOKEN: ${{ secrets.ALERT_TELEGRAM_BOT_TOKEN }}" in job
    assert "ALERT_TELEGRAM_CHAT_ID: ${{ vars.ALERT_TELEGRAM_CHAT_ID }}" in job
    assert "ALERT_TELEGRAM" not in _job("deploy-staging")


# --- deploy.sh on a fake host --------------------------------------------------------------

FAKE_DOCKER = """\
#!/usr/bin/env bash
# Fake docker CLI: logs every call; psql and grafana stdin go to their own logs.
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  network) exit 0 ;;
  ps) echo "${STUB_POSTGRES_ID}" ;;
  exec) cat >> "${STUB_PSQL_STDIN}"; printf '\\n--\\n' >> "${STUB_PSQL_STDIN}" ;;
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
        self, *, unset: tuple[str, ...] = (), **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "API_DIR": str(self.api_dir),
            "GRAFANA_ROOT_URL": "https://grafana.example.test/",
            "ALERT_TELEGRAM_BOT_TOKEN": TOKEN,
            "ALERT_TELEGRAM_CHAT_ID": CHAT_ID,
            "STUB_LOG": str(self.stubs / "docker.log"),
            "STUB_PSQL_STDIN": str(self.stubs / "psql.log"),
            "STUB_GRAFANA_STDIN": str(self.stubs / "grafana.log"),
            "STUB_CHOWN_LOG": str(self.stubs / "chown.log"),
            "STUB_POSTGRES_ID": "pgcontainer",
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
    for generated in (".env", "alertmanager/alertmanager.yml"):
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
    }
    for key in ("GRAFANA_ADMIN_PASSWORD", "GRAFANA_DB_PASSWORD", "PG_EXPORTER_PASSWORD"):
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
        TOKEN,
    )

    assert result.returncode == 0, result.stderr
    assert f"ALTER ROLE grafana_reader PASSWORD '{env['GRAFANA_DB_PASSWORD']}';" in host.log(
        "psql.log"
    )
    assert f"ALTER ROLE metrics_exporter PASSWORD '{env['PG_EXPORTER_PASSWORD']}';" in host.log(
        "psql.log"
    )
    assert host.log("grafana.log") == env["GRAFANA_ADMIN_PASSWORD"]
    for secret in secrets:
        assert secret not in host.log("docker.log")  # argv of every docker call
        assert secret not in result.stdout + result.stderr
    assert "exec -i pgcontainer psql -X -q -v ON_ERROR_STOP=1 -U app -d app" in host.log(
        "docker.log"
    )


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
