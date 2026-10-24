"""Acceptance evidence of the monitoring stack (deploy/monitoring/acceptance.sh, #186).

The behavioural tests run the real script against a fake docker CLI that answers like the staging
host: what each check reports, that a failed check fails the run only after every other check ran,
that the scratch database is always dropped and that no password reaches argv or the report. The
static tests read the manual workflow monitoring-acceptance.yml.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "monitoring" / "acceptance.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "monitoring-acceptance.yml"
API_PROJECT = "dmc-268-api-staging"
GRAFANA_DB_PASSWORD = "a" * 48
PG_EXPORTER_PASSWORD = "b" * 48
DUMP = "app-20261024T030000Z.dump"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records argv, answers like the staging host. Behaviour knobs: STUB_* variables.
printf '%s\n' "$*" >> "${STUB_LOG}"
query=""
prev=""
for arg in "$@"; do
  if [[ "${prev}" == "-c" ]]; then query="${arg}"; fi
  prev="${arg}"
done
case " $* " in
  *" ps -q "*) echo pgcontainer ;;
  *" ps --format "*)
    printf 'prometheus running healthy\nalertmanager running healthy\ngrafana running healthy\n'
    printf 'pg-backup running healthy\n'
    ;;
  *" promtool query instant "*)
    case "${query}${*}" in
      *rabbitmq_detailed_queue_messages_ready*)
        echo '{queue="review.run.fast"} => 0 @[1761300000]'
        echo '{queue="reviews.dlq"} => 2 @[1761300000]'
        ;;
      *caddy_http_request_duration_seconds_count*)
        echo '{host="staging-api.example.test"} => 0.4 @[1761300000]' ;;
      *)
        echo 'up{instance="prometheus:9090", job="prometheus"} => 1 @[1761300000]'
        pg_up="${STUB_POSTGRES_UP:-1}"
        echo "up{instance=\"postgres-exporter:9187\", job=\"postgres\"} => ${pg_up} @[1761300000]"
        ;;
    esac
    ;;
  *" psql "*)
    if [[ -n "${PGPASSWORD:-}" ]]; then echo "${PGPASSWORD}" >> "${STUB_PGPASSWORD_LOG}"; fi
    case "${query}" in
      *"request FROM run_actions"*|*"review_body FROM runs"*)
        if [[ -n "${STUB_CONTENT_READABLE:-}" ]]; then echo "leaked"; exit 0; fi
        echo "ERROR:  permission denied for table ${query##* FROM }" | sed 's/ LIMIT 1//' >&2
        exit 1
        ;;
      *"count(run_id)"*) echo 42 ;;
      *"percentile_cont"*) echo "17 3.2 9.8" ;;
      *alembic_version*) echo 20261020_0037 ;;
      *pg_tables*) echo 31 ;;
      *"FROM runs"*) echo 12 ;;
    esac
    ;;
  *" createdb "*|*" dropdb "*) ;;
  *" pg_restore "*)
    cat > /dev/null
    if [[ -n "${STUB_RESTORE_FAILS:-}" ]]; then echo "pg_restore: error: bad dump" >&2; exit 1; fi
    ;;
  *" amtool "*) touch "${STUB_ALERT_SENT}" ;;
  *" wget "*)
    sent=3 failed=0
    if [[ -f "${STUB_ALERT_SENT}" ]]; then
      if [[ -n "${STUB_DELIVERY_FAILS:-}" ]]; then
        failed=1
      elif [[ -z "${STUB_NO_DELIVERY:-}" ]]; then
        sent=4
      fi
    fi
    tg='integration="telegram"'
    echo "alertmanager_notifications_total{${tg},receiver_name=\"telegram\"} ${sent}"
    echo "alertmanager_notifications_failed_total{${tg},reason=\"other\"} ${failed}"
    echo "alertmanager_notifications_total{integration=\"email\"} 9"
    ;;
esac
"""


@dataclass
class Run:
    result: subprocess.CompletedProcess[str]
    calls: list[str]
    pgpasswords: str

    @property
    def report(self) -> str:
        return self.result.stdout

    def line(self, check: str) -> str:
        lines = [x for x in self.report.splitlines() if f"  {check}  " in f"  {x[6:]}"]
        assert lines, f"no summary line for {check}:\n{self.report}"
        return lines[-1]


def _run(
    tmp_path: Path,
    *,
    dump_age_hours: float = 3,
    offsite: bool = True,
    send_test_alert: str = "1",
    **env: str,
) -> Run:
    api_dir = tmp_path / API_PROJECT
    api_dir.mkdir(exist_ok=True)
    (api_dir / ".env").write_text("POSTGRES_USER=app\nPOSTGRES_DB=app\n", encoding="utf-8")
    monitoring = tmp_path / "monitoring"
    (monitoring / "alertmanager").mkdir(parents=True, exist_ok=True)
    (monitoring / "alertmanager" / "alertmanager.yml").write_text(
        "receivers:\n  - name: telegram\n    telegram_configs: []\n", encoding="utf-8"
    )
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "app-20261023T030000Z.dump").write_bytes(b"older")
    (backups / DUMP).write_bytes(b"PGDMP")
    textfile = tmp_path / "textfile"
    textfile.mkdir(exist_ok=True)
    now = int(time.time())
    (textfile / "pg_backup.prom").write_text(
        f"dmc_pg_backup_last_success_timestamp_seconds {now - int(dump_age_hours * 3600)}\n"
        "dmc_pg_backup_last_size_bytes 112640\n",
        encoding="utf-8",
    )
    (textfile / "pg_backup_offsite.prom").write_text(
        f"dmc_pg_backup_offsite_last_success_timestamp_seconds {now - 600}\n", encoding="utf-8"
    )
    settings = [
        "POSTGRES_DB=app",
        f"GRAFANA_DB_PASSWORD={GRAFANA_DB_PASSWORD}",
        f"PG_EXPORTER_PASSWORD={PG_EXPORTER_PASSWORD}",
        f"BACKUP_DIR={backups}",
        f"TEXTFILE_DIR={textfile}",
    ]
    if offsite:
        settings.append("BACKUP_S3_BUCKET=dmc268-t6-backup")
    (monitoring / ".env").write_text("\n".join(settings) + "\n", encoding="utf-8")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (stubs / "docker").chmod(0o755)
    log = stubs / "docker.log"
    pgpasswords = stubs / "pgpasswords.log"
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env={
            "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "API_DIR": str(api_dir),
            "MONITORING_DIR": str(monitoring),
            "SEND_TEST_ALERT": send_test_alert,
            "RUN_URL": "https://github.com/example/actions/runs/1",
            "STUB_LOG": str(log),
            "STUB_PGPASSWORD_LOG": str(pgpasswords),
            "STUB_ALERT_SENT": str(stubs / "alert-sent"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return Run(
        result,
        log.read_text(encoding="utf-8").splitlines() if log.exists() else [],
        pgpasswords.read_text(encoding="utf-8") if pgpasswords.exists() else "",
    )


def test_healthy_stack_passes_every_check(tmp_path: Path) -> None:
    run = _run(tmp_path)

    assert run.result.returncode == 0, run.report + run.result.stderr
    assert run.report.rstrip().endswith("all checks passed")
    for check in (
        "stack services",
        "prometheus targets",
        "LLM latency (Grafana source)",
        "queue size (RabbitMQ)",
        "RPS (edge proxy)",
        "role isolation",
        "verified dump < 26 h",
        "off-host copy (R2) < 3 h",
        "restore drill",
        "test alert",
    ):
        assert run.line(check).startswith("OK"), run.line(check)
    assert "17 calls in 7 days, p95 9.8 s" in run.line("LLM latency (Grafana source)")
    assert f"{DUMP} -> revision 20261020_0037, 31 tables, 12 runs" in run.line("restore drill")


def test_report_shows_permission_denied_for_both_monitoring_roles(tmp_path: Path) -> None:
    run = _run(tmp_path)

    for role in ("grafana_reader", "metrics_exporter"):
        assert (
            f"{role}: SELECT request FROM run_actions LIMIT 1 -> "
            "ERROR:  permission denied for table run_actions"
        ) in run.report
        assert (
            f"{role}: SELECT review_body FROM runs LIMIT 1 -> "
            "ERROR:  permission denied for table runs"
        ) in run.report
        assert f"{role}: SELECT count(run_id) FROM run_actions -> 42" in run.report


def test_passwords_reach_psql_through_the_environment_only(tmp_path: Path) -> None:
    run = _run(tmp_path)

    # Both roles connected with their own password, which never appears in argv or the report.
    assert GRAFANA_DB_PASSWORD in run.pgpasswords
    assert PG_EXPORTER_PASSWORD in run.pgpasswords
    for password in (GRAFANA_DB_PASSWORD, PG_EXPORTER_PASSWORD):
        assert all(password not in call for call in run.calls)
        assert password not in run.report + run.result.stderr
    assert any(call.startswith("exec -i -e PGPASSWORD pgcontainer psql") for call in run.calls)


def test_restore_drill_uses_the_newest_dump_and_drops_the_scratch_database(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    drill = [c for c in run.calls if "restore_drill_acceptance" in c or "pg_restore" in c]

    assert "createdb -U app -O app restore_drill_acceptance" in drill[1]
    assert "pg_restore -U app -d restore_drill_acceptance --no-owner --role=app" in drill[2]
    assert "--exit-on-error --single-transaction" in drill[2]
    assert "dropdb -U app --if-exists restore_drill_acceptance" in run.calls[-1]
    assert f"newest dump: {DUMP}" in run.report


def test_test_alert_goes_to_the_event_receiver_and_waits_for_delivery(tmp_path: Path) -> None:
    run = _run(tmp_path)
    [amtool] = [c for c in run.calls if " amtool " in c]

    assert "exec -T alertmanager amtool alert add MonitoringAcceptanceTest" in amtool
    assert "severity=info kind=event environment=staging" in amtool
    assert '--annotation=description="https://github.com/example/actions/runs/1"' in amtool
    assert "telegram notifications: sent 3 -> 4, failed 0 -> 0" in run.report


def test_failed_check_fails_the_run_after_every_other_check(tmp_path: Path) -> None:
    run = _run(tmp_path, STUB_CONTENT_READABLE="1")

    assert run.result.returncode == 1
    assert run.line("role isolation").startswith("FAIL")
    # The run log is public: a leaked value is never printed.
    assert "leaked" not in run.report + run.result.stderr
    assert "SELECT request FROM run_actions LIMIT 1 -> NOT DENIED (output hidden)" in run.report
    assert run.line("restore drill").startswith("OK")
    assert run.line("test alert").startswith("OK")
    assert "failed: role isolation" in run.report


@pytest.mark.parametrize(
    ("kwargs", "check", "detail"),
    [
        ({"dump_age_hours": 30}, "verified dump < 26 h", "last verified dump is 30h 00m old"),
        ({"offsite": False}, "off-host copy (R2) < 3 h", "BACKUP_S3_* are not set"),
        ({"STUB_POSTGRES_UP": "0"}, "prometheus targets", "down: postgres"),
        ({"STUB_RESTORE_FAILS": "1"}, "restore drill", f"pg_restore of {DUMP} failed"),
        ({"STUB_DELIVERY_FAILS": "1"}, "test alert", "failed to deliver to Telegram"),
        (
            {"STUB_NO_DELIVERY": "1", "ALERT_DELIVERY_TIMEOUT_SECONDS": "0"},
            "test alert",
            "no Telegram notification within 0 s",
        ),
    ],
)
def test_each_failure_is_reported_by_its_check(
    tmp_path: Path, kwargs: dict[str, object], check: str, detail: str
) -> None:
    run = _run(tmp_path, **kwargs)  # type: ignore[arg-type]

    assert run.result.returncode == 1
    assert run.line(check).startswith("FAIL")
    assert detail in run.line(check)
    # The scratch database is dropped whatever failed.
    assert "dropdb -U app --if-exists restore_drill_acceptance" in run.calls[-1]


def test_test_alert_can_be_skipped(tmp_path: Path) -> None:
    run = _run(tmp_path, send_test_alert="0")

    assert run.result.returncode == 0, run.report
    assert run.line("test alert").startswith("SKIP")
    assert not any(" amtool " in call for call in run.calls)


def test_workflow_runs_only_by_hand_on_main_with_read_permissions() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "on:\n  workflow_dispatch:\n" in workflow
    assert "\n  push:" not in workflow and "pull_request" not in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "      name: staging\n" in workflow
    # Not in staging-monitoring: a pending run there would replace a pending Deploy monitoring.
    assert "'staging-monitoring-acceptance'" in workflow
    assert "group: staging-monitoring\n" not in workflow
    # Pinned actions, like the deploy jobs.
    assert "appleboy/scp-action@ff85246acaad7bdce478db94a363cd2bf7c90345" in workflow
    assert "appleboy/ssh-action@0ff4204d59e8e51228ff73bce53f80d53301dee2" in workflow
    assert "fingerprint: ${{ vars.STAGING_SSH_FINGERPRINT }}" in workflow
    assert 'source: "deploy/monitoring/acceptance.sh"' in workflow
    assert "envs: MONITORING_DIR,API_DIR,SEND_TEST_ALERT,RUN_URL\n" in workflow
