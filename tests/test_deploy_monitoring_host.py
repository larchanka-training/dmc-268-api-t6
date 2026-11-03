"""deploy/monitoring/deploy.sh on a fake host: secrets, alert config, probes and roles."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests.monitoring_deploy_host import (
    API_PROJECT,
    CHAT_ID,
    METADATA_GRANTS,
    MONITORING,
    TOKEN,
    Host,
    _grants,
    _job,
    _read,
)

pytest_plugins = ("tests.monitoring_deploy_host",)

# --- deploy.sh on a fake host --------------------------------------------------------------


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
