"""Acceptance evidence of the monitoring stack (deploy/monitoring/acceptance.sh, #186).

The behavioural tests run the real script against a fake docker CLI that answers like the pinned
images on the staging host (checked on prom/alertmanager:v0.34.1 and postgres:17.11-alpine):

- Alertmanager counts a notification in notifications_total before the request and in
  notifications_failed_total only after every retry; whether Telegram accepted it shows only in
  notification_requests_total and notification_requests_failed_total;
- psql exits with 1 and prints "ERROR:  permission denied for table <t>" for a missing grant;
- dropdb without --force fails while a session holds the database.

They check what each check reports, that one failure fails only its own check, that the run always
reaches its summary, that the scratch database is dropped or the run fails, and that no password or
row content reaches argv or the report. The static tests read the workflow.
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
# Row content of a closed column that happens to contain the phrase psql prints for a missing grant.
LEAKED_ROW = "review said: permission denied for the secret token abc123 in run"
CHECKS = (
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
)

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records argv and answers like the staging host. Behaviour knobs: STUB_* variables.
printf '%s\n' "$*" >> "${STUB_LOG}"
query=""
prev=""
for arg in "$@"; do
  if [[ "${prev}" == "-c" ]]; then query="${arg}"; fi
  prev="${arg}"
done
state="${STUB_STATE}"
count() { cat "${state}/$1" 2>/dev/null || echo 0; }
bump() { echo $(($(count "$1") + 1)) > "${state}/$1"; }
case " $* " in
  *" ps -q "*) echo pgcontainer ;;
  *" inspect "*) echo 172.18.0.5 ;;
  *" config --services "*)
    if [[ -n "${STUB_COMPOSE_FAILS:-}" ]]; then echo "Error: compose.yml not found" >&2; exit 1; fi
    printf 'prometheus\nalertmanager\ngrafana\npg-backup\n'
    if [[ " $* " == *compose.offsite.yml* ]]; then echo backup-offsite; fi
    ;;
  *" ps -a --format "*)
    printf 'prometheus running healthy\nalertmanager running healthy\ngrafana running healthy\n'
    if [[ -z "${STUB_PG_BACKUP_EXITED:-}" ]]; then
      echo 'pg-backup running healthy'
    else
      echo 'pg-backup exited '
    fi
    echo 'backup-offsite running healthy'
    ;;
  *" promtool query instant "*)
    if [[ -n "${STUB_PROMTOOL_FAILS:-}" ]]; then echo "Error: connection refused" >&2; exit 1; fi
    case "$*" in
      *rabbitmq_detailed_queue_messages_ready*)
        echo '{queue="review.run.fast"} => 0 @[1761300000]'
        if [[ -z "${STUB_NO_PUBLISH_QUEUE:-}" ]]; then
          echo '{queue="review.publish"} => 1 @[1761300000]'
        fi
        ;;
      *caddy_http_request_duration_seconds_count*)
        if [[ -z "${STUB_NO_RPS:-}" ]]; then
          echo '{host="staging-api.example.test"} => 0.4 @[1761300000]'
        fi
        ;;
      *)
        pg_up="${STUB_POSTGRES_UP:-1}"
        echo 'up{instance="prometheus:9090", job="prometheus"} => 1 @[1761300000]'
        echo "up{instance=\"postgres-exporter:9187\", job=\"postgres\"} => ${pg_up} @[1761300000]"
        ;;
    esac
    ;;
  *" psql "*)
    if [[ -n "${PGPASSWORD:-}" ]]; then echo "${PGPASSWORD}" >> "${STUB_PGPASSWORD_LOG}"; fi
    case "${query}" in
      *"request FROM run_actions"*|*"review_body FROM runs"*)
        if [[ -n "${STUB_CONTENT_READABLE:-}" ]]; then echo "${STUB_CONTENT_READABLE}"; exit 0; fi
        if [[ -n "${STUB_CLOSED_ERROR:-}" ]]; then echo "${STUB_CLOSED_ERROR}" >&2; exit 1; fi
        table="${query##* FROM }"
        echo "ERROR:  permission denied for table ${table%% *}" >&2
        exit 1
        ;;
      *"count(run_id)"*)
        if [[ -n "${STUB_METADATA_DENIED:-}" ]]; then
          echo "ERROR:  permission denied for table run_actions" >&2
          exit 1
        fi
        echo 42
        ;;
      *"percentile_cont"*)
        if [[ -n "${STUB_LLM_FAILS:-}" ]]; then
          echo "psql: error: FATAL:  password authentication failed for user \"grafana_reader\"" >&2
          exit 2
        fi
        echo "${STUB_LLM:-17 3.2 9.8}"
        ;;
      *alembic_version*)
        if [[ -n "${STUB_NO_ALEMBIC:-}" && " $* " == *restore_drill_acceptance* ]]; then
          echo 'ERROR:  relation "alembic_version" does not exist' >&2
          exit 1
        fi
        echo 20261020_0037
        ;;
      *pg_database*) if [[ -n "${STUB_DROP_FAILS:-}" ]]; then echo 1; else echo 0; fi ;;
      *pg_tables*) echo 31 ;;
      *"FROM runs"*) echo 12 ;;
    esac
    ;;
  *" createdb "*) ;;
  *" dropdb "*)
    if [[ -n "${STUB_DROP_FAILS:-}" ]]; then
      echo 'dropdb: error: database removal failed: ERROR:  database is being accessed' >&2
      exit 1
    fi
    ;;
  *" pg_restore "*)
    cat > /dev/null
    if [[ -n "${STUB_RESTORE_FAILS:-}" ]]; then
      echo 'pg_restore: error: could not execute query: ERROR:  duplicate key value' >&2
      echo 'DETAIL:  Failing row contains (secret row content).' >&2
      exit 1
    fi
    ;;
  *" amtool "*)
    if [[ -n "${STUB_AMTOOL_FAILS:-}" ]]; then echo "Error: connection refused" >&2; exit 1; fi
    touch "${state}/sent"
    ;;
  *" wget "*)
    if [[ -n "${STUB_WGET_FAILS:-}" ]]; then echo "wget: can't connect" >&2; exit 1; fi
    if [[ -f "${state}/sent" ]]; then
      bump reads
      reads="$(count reads)"
      # Attempts count at once, whatever Telegram answers (notify/retry_stage.go).
      echo 1 > "${state}/attempts"
      # STUB_TELEGRAM: ok (accepts from read STUB_DELIVER_ON_READ), down (every request fails),
      # retry (first request fails, the next one is accepted), silent (no request yet).
      case "${STUB_TELEGRAM:-ok}" in
        ok) if ((reads >= ${STUB_DELIVER_ON_READ:-1})); then echo 1 > "${state}/requests"; fi ;;
        down) echo "${reads}" > "${state}/requests"; echo "${reads}" > "${state}/failed" ;;
        retry)
          echo 1 > "${state}/failed"
          requests=1
          if ((reads >= 2)); then requests=2; fi
          echo "${requests}" > "${state}/requests"
          ;;
      esac
    fi
    tg='integration="telegram"'
    echo "alertmanager_notifications_total{${tg}} $(($(count attempts) + 3))"
    echo "alertmanager_notifications_failed_total{${tg},reason=\"other\"} 0"
    echo "alertmanager_notification_requests_total{${tg}} $(($(count requests) + 5))"
    echo "alertmanager_notification_requests_failed_total{${tg}} $(($(count failed) + 2))"
    echo "alertmanager_notification_requests_total{integration=\"email\"} 9"
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
        summary = self.report[self.report.index("== Summary") :]
        lines = [x for x in summary.splitlines() if x[6:].startswith(check + "  ")]
        assert len(lines) == 1, f"no summary line for {check}:\n{self.report}"
        return lines[0]

    def status(self, check: str) -> str:
        return self.line(check).split()[0]


def _run(
    tmp_path: Path,
    *,
    dump_age_hours: float = 3,
    offsite_age_minutes: float | None = 10,
    offsite: bool = True,
    dumps: bool = True,
    telegram: bool = True,
    deploy_mode: str = "edge",
    send_test_alert: str = "1",
    extra: dict[str, str] | None = None,
    **env: str,
) -> Run:
    api_dir = tmp_path / API_PROJECT
    api_dir.mkdir(exist_ok=True)
    (api_dir / ".env").write_text(
        f"POSTGRES_USER=app\nPOSTGRES_DB=app\nDEPLOY_MODE={deploy_mode}\n", encoding="utf-8"
    )
    monitoring = tmp_path / "monitoring"
    (monitoring / "alertmanager").mkdir(parents=True, exist_ok=True)
    receiver = "    telegram_configs: []\n" if telegram else ""
    (monitoring / "alertmanager" / "alertmanager.yml").write_text(
        "receivers:\n  - name: telegram\n" + receiver, encoding="utf-8"
    )
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    if dumps:
        (backups / "app-20261023T030000Z.dump").write_bytes(b"older")
        (backups / DUMP).write_bytes(b"PGDMP")
    textfile = tmp_path / "textfile"
    textfile.mkdir(exist_ok=True)
    now = int(time.time())
    if dumps:
        (textfile / "pg_backup.prom").write_text(
            f"dmc_pg_backup_last_success_timestamp_seconds {now - int(dump_age_hours * 3600)}\n"
            "dmc_pg_backup_last_size_bytes 112640\n",
            encoding="utf-8",
        )
    if offsite_age_minutes is not None:
        (textfile / "pg_backup_offsite.prom").write_text(
            "dmc_pg_backup_offsite_last_success_timestamp_seconds "
            f"{now - int(offsite_age_minutes * 60)}\n",
            encoding="utf-8",
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
    state = stubs / "state"
    state.mkdir(parents=True, exist_ok=True)
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
            "RUN_ID": "1-1",
            "ALERT_POLL_SECONDS": "0",
            "ALERT_DELIVERY_TIMEOUT_SECONDS": "2",
            "STUB_LOG": str(log),
            "STUB_PGPASSWORD_LOG": str(pgpasswords),
            "STUB_STATE": str(state),
            **(extra or {}),
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


def _assert_only_failed(run: Run, *failed: str) -> None:
    assert run.result.returncode == 1, run.report
    for check in CHECKS:
        expected = "FAIL" if check in failed else "OK"
        assert run.status(check) == expected, run.line(check)
    assert run.report.rstrip().splitlines()[-1] == "failed: " + ",".join(
        check for check in CHECKS if check in failed
    )


def test_healthy_stack_passes_every_check(tmp_path: Path) -> None:
    run = _run(tmp_path)

    assert run.result.returncode == 0, run.report + run.result.stderr
    assert run.report.rstrip().splitlines()[-1] == "all checks passed"
    for check in CHECKS:
        assert run.status(check) == "OK", run.line(check)
    assert "17 calls in 7 days, p95 9.8 s" in run.line("LLM latency (Grafana source)")
    assert f"{DUMP} -> revision 20261020_0037, 31 tables, 12 runs" in run.line("restore drill")
    assert "5 expected services running" in run.line("stack services")
    assert "scratch database restore_drill_acceptance dropped" in run.report


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


def test_monitoring_roles_connect_over_the_network_with_their_passwords(tmp_path: Path) -> None:
    run = _run(tmp_path)
    role_calls = [c for c in run.calls if c.startswith("exec -i -e PGPASSWORD pgcontainer psql")]

    # Not 127.0.0.1: loopback is trust in the postgres image and accepts any password.
    assert role_calls
    assert all(" -h 172.18.0.5 " in call for call in role_calls)
    assert GRAFANA_DB_PASSWORD in run.pgpasswords
    assert PG_EXPORTER_PASSWORD in run.pgpasswords
    for password in (GRAFANA_DB_PASSWORD, PG_EXPORTER_PASSWORD):
        assert all(password not in call for call in run.calls)
        assert password not in run.report + run.result.stderr


def test_readable_closed_column_fails_and_its_content_is_never_printed(tmp_path: Path) -> None:
    # The content contains the very phrase of a denied grant: only the exit code decides.
    run = _run(tmp_path, STUB_CONTENT_READABLE=LEAKED_ROW)

    _assert_only_failed(run, "role isolation")
    assert "abc123" not in run.report + run.result.stderr
    assert "SELECT request FROM run_actions LIMIT 1 -> NOT DENIED (output hidden)" in run.report
    assert "grafana_reader: SELECT request readable" in run.line("role isolation")


def test_only_the_error_of_a_missing_column_grant_counts_as_denied(tmp_path: Path) -> None:
    # Another error, even one with the same words, is no proof that the grant is closed.
    run = _run(tmp_path, STUB_CLOSED_ERROR="ERROR:  permission denied for schema public")

    _assert_only_failed(run, "role isolation")
    assert "SELECT request failed without permission denied" in run.line("role isolation")


def test_restore_drill_uses_the_newest_dump_and_drops_the_scratch_database(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    drill = [c for c in run.calls if "restore_drill_acceptance" in c or "pg_restore" in c]

    assert "dropdb -U app --force --if-exists restore_drill_acceptance" in drill[0]
    assert "createdb -U app -O app restore_drill_acceptance" in drill[1]
    assert "pg_restore -U app -d restore_drill_acceptance --no-owner --role=app" in drill[2]
    assert "--exit-on-error --single-transaction" in drill[2]
    # The regular drop, the check that the database is gone, then the safety net of the EXIT trap.
    assert "dropdb -U app --force --if-exists restore_drill_acceptance" in drill[-3]
    assert "pg_database WHERE datname = 'restore_drill_acceptance'" in drill[-2]
    assert "dropdb -U app --force --if-exists restore_drill_acceptance" in drill[-1]
    assert f"newest dump: {DUMP}" in run.report


def test_scratch_database_left_on_the_host_fails_the_run(tmp_path: Path) -> None:
    run = _run(tmp_path, STUB_DROP_FAILS="1")

    assert run.result.returncode == 1
    assert run.status("scratch db cleanup") == "FAIL"
    assert "scratch database restore_drill_acceptance dropped" not in run.report
    assert "was not dropped" in run.report


def test_failed_restore_prints_the_error_line_without_its_detail(tmp_path: Path) -> None:
    run = _run(tmp_path, STUB_RESTORE_FAILS="1")

    _assert_only_failed(run, "restore drill")
    assert "pg_restore: error: could not execute query" in run.line("restore drill")
    assert "secret row content" not in run.report + run.result.stderr
    assert "scratch database restore_drill_acceptance dropped" in run.report


def test_restore_without_a_revision_fails(tmp_path: Path) -> None:
    run = _run(tmp_path, STUB_NO_ALEMBIC="1")

    _assert_only_failed(run, "restore drill")
    assert "revision 20261020" not in run.line("restore drill")


def test_test_alert_is_a_new_event_alert_of_this_run(tmp_path: Path) -> None:
    run = _run(tmp_path)
    [amtool] = [c for c in run.calls if " amtool " in c]

    assert "exec -T alertmanager amtool alert add MonitoringAcceptanceTest" in amtool
    assert "severity=info kind=event environment=staging run_id=1-1" in amtool
    assert '--annotation=description="https://github.com/example/actions/runs/1"' in amtool
    assert "Telegram accepted the notification" in run.line("test alert")


def test_alert_waits_for_a_later_accepted_request(tmp_path: Path) -> None:
    run = _run(tmp_path, STUB_DELIVER_ON_READ="3", ALERT_DELIVERY_TIMEOUT_SECONDS="30")
    reads = [c for c in run.calls if " wget " in c]

    assert run.status("test alert") == "OK", run.line("test alert")
    # One read before the alert, then three until the request was accepted.
    assert len(reads) == 4


def test_failed_first_request_with_a_successful_retry_is_a_delivery(tmp_path: Path) -> None:
    run = _run(tmp_path, STUB_TELEGRAM="retry", ALERT_DELIVERY_TIMEOUT_SECONDS="30")

    assert run.status("test alert") == "OK", run.line("test alert")


@pytest.mark.parametrize(
    ("kwargs", "check", "detail"),
    [
        ({"dump_age_hours": 30}, "verified dump < 26 h", "last verified dump is 30h 00m old"),
        ({"offsite_age_minutes": 240}, "off-host copy (R2) < 3 h", "last successful copy 4h 00m"),
        ({"offsite_age_minutes": None}, "off-host copy (R2) < 3 h", "no successful copy yet"),
        ({"STUB_POSTGRES_UP": "0"}, "prometheus targets", "down: postgres"),
        ({"STUB_PG_BACKUP_EXITED": "1"}, "stack services", "pg-backup exited"),
        ({"STUB_LLM": "0 0 0"}, "LLM latency (Grafana source)", "no llm.call in 7 days"),
        ({"STUB_LLM_FAILS": "1"}, "LLM latency (Grafana source)", "query as grafana_reader"),
        ({"STUB_NO_PUBLISH_QUEUE": "1"}, "queue size (RabbitMQ)", "review.publish"),
        ({"STUB_NO_RPS": "1"}, "RPS (edge proxy)", "no caddy_http_request"),
        ({"STUB_METADATA_DENIED": "1"}, "role isolation", "cannot read its metadata columns"),
        ({"STUB_TELEGRAM": "down"}, "test alert", "every request to Telegram failed"),
        ({"STUB_TELEGRAM": "silent"}, "test alert", "no request to Telegram within 2 s"),
        ({"STUB_AMTOOL_FAILS": "1"}, "test alert", "amtool alert add failed: Error:"),
        ({"telegram": False}, "test alert", "Telegram is not configured"),
    ],
)
def test_each_failure_fails_only_its_own_check(
    tmp_path: Path, kwargs: dict[str, object], check: str, detail: str
) -> None:
    run = _run(tmp_path, **kwargs)  # type: ignore[arg-type]

    _assert_only_failed(run, check)
    assert detail in run.line(check)
    assert "scratch database restore_drill_acceptance dropped" in run.report


def test_without_the_off_host_copy_its_service_is_not_expected(tmp_path: Path) -> None:
    run = _run(tmp_path, offsite=False)

    _assert_only_failed(run, "off-host copy (R2) < 3 h")
    assert "BACKUP_S3_* are not set" in run.line("off-host copy (R2) < 3 h")
    assert "4 expected services running" in run.line("stack services")


def test_without_dumps_both_backup_checks_fail(tmp_path: Path) -> None:
    run = _run(tmp_path, dumps=False)

    _assert_only_failed(run, "verified dump < 26 h", "restore drill")
    assert "no dump to restore" in run.line("restore drill")


@pytest.mark.parametrize(
    ("knob", "failed"),
    [
        ("STUB_COMPOSE_FAILS", ("stack services",)),
        (
            "STUB_PROMTOOL_FAILS",
            ("prometheus targets", "queue size (RabbitMQ)", "RPS (edge proxy)"),
        ),
        ("STUB_WGET_FAILS", ("test alert",)),
    ],
)
def test_a_failing_tool_still_reaches_the_summary(
    tmp_path: Path, knob: str, failed: tuple[str, ...]
) -> None:
    run = _run(tmp_path, extra={knob: "1"})

    _assert_only_failed(run, *failed)
    assert "== Summary" in run.report


def test_ports_mode_skips_the_edge_proxy_rps(tmp_path: Path) -> None:
    run = _run(tmp_path, deploy_mode="ports")

    assert run.result.returncode == 0, run.report
    assert run.status("RPS (edge proxy)") == "SKIP"
    assert not any("caddy_http_request" in call for call in run.calls)


def test_test_alert_can_be_skipped(tmp_path: Path) -> None:
    run = _run(tmp_path, send_test_alert="0")

    assert run.result.returncode == 0, run.report
    assert run.status("test alert") == "SKIP"
    assert not any(" amtool " in call for call in run.calls)


def test_report_names_neither_the_host_nor_the_bucket(tmp_path: Path) -> None:
    run = _run(tmp_path)

    assert "dmc268-t6-backup" not in run.report
    assert subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip() not in (
        run.report
    )


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
    assert "envs: MONITORING_DIR,API_DIR,SEND_TEST_ALERT,RUN_URL,RUN_ID\n" in workflow
    # The alert waits up to 6 min: longer than the default 10 min of the action is not needed,
    # shorter than the job timeout is.
    assert "command_timeout: 20m" in workflow
    assert "timeout-minutes: 25" in workflow


def test_workflow_runs_the_host_checks_and_the_report_after_a_grafana_failure() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    grafana = workflow[workflow.index("- name: Public Grafana endpoints") :]
    grafana = grafana[: grafana.index("- name: Upload acceptance script")]
    report = workflow[workflow.index("- name: Report") :]

    assert "exit 1" not in grafana
    assert "--max-time 20" in grafana
    assert 'echo "result=fail" >> "${GITHUB_OUTPUT}"' in grafana
    assert "if: ${{ !cancelled() && steps.target.outcome == 'success' }}" in report
    # The verdict is the last line of the report, not a phrase anywhere in it.
    assert "tail -n 1" in report
    assert '"${last}" != "all checks passed"' in report
    assert '"${GRAFANA}" == fail' in report
