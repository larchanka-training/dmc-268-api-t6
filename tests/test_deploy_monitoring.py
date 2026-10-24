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

    assert len(images) == 8
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
    # Only what talks to postgres, rabbitmq or redis joins the application network.
    on_api = {name for name, block in blocks.items() if "      api: {}" in block}
    assert on_api == {"prometheus", "grafana", "postgres-exporter", "redis-exporter", "pg-backup"}
    # The backup job needs PostgreSQL only.
    assert "default: {}" not in blocks["pg-backup"]
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
        "pg-backup": "PG_BACKUP_CONFIG_DIGEST",
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
    # Its own lock: in staging-deploy a pending monitoring job and promote-staging could cancel
    # each other when the next merge queues its deploy.
    assert re.findall(r"^      group: (\S+)$", job, re.M) == ["staging-monitoring"]


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
# Fake docker CLI: logs every call; psql, grafana and rclone obscure stdin go to their own logs.
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1" in
  network) exit 0 ;;
  run) cat >> "${STUB_OBSCURE_STDIN}"; echo "obscured_${RANDOM}${RANDOM}" ;;
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
            "ALERT_TELEGRAM_BOT_TOKEN": TOKEN,
            "ALERT_TELEGRAM_CHAT_ID": CHAT_ID,
            "STUB_LOG": str(self.stubs / "docker.log"),
            "STUB_PSQL_STDIN": str(self.stubs / "psql.log"),
            "STUB_GRAFANA_STDIN": str(self.stubs / "grafana.log"),
            "STUB_CHOWN_LOG": str(self.stubs / "chown.log"),
            "STUB_POSTGRES_ID": "pgcontainer",
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


GRAFANA_SECRET = "Team-Grafana_pass.2026+x/y="


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
    assert stored == GRAFANA_SECRET
    assert kept.returncode == 0, kept.stderr
    assert host.env()["GRAFANA_ADMIN_PASSWORD"] == GRAFANA_SECRET
    # Synced through stdin on every deploy, never in argv or the job log.
    assert host.log("grafana.log").endswith(GRAFANA_SECRET * 2)
    assert GRAFANA_SECRET not in host.log("docker.log")
    for result in (from_secret, kept):
        assert GRAFANA_SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "password", ["short-password", "has$dollar-in-the-middle", "with space inside it"]
)
def test_invalid_grafana_admin_password_fails_before_the_host_changes(
    host: Host, password: str
) -> None:
    result = host.deploy(GRAFANA_ADMIN_PASSWORD=password)

    assert result.returncode == 1
    assert "GRAFANA_ADMIN_PASSWORD must be 16-128 characters" in result.stderr
    assert not host.env_file.exists()
    assert host.log("psql.log") == ""
    assert password not in result.stdout + result.stderr


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

    assert events == ["ReviewRunsFailed", "ReviewDeadLetters"]
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
