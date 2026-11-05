"""pg-backup.sh and the off-host copy of the dumps of the monitoring stack."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.monitoring_deploy_host import (
    MONITORING,
    NO_OFFSITE,
    OFFSITE,
    PASSPHRASE,
    Host,
    _job,
    _read,
)

pytest_plugins = ("tests.monitoring_deploy_host",)

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
    textfile: str = ""
    heartbeat: str = ""


def _metric_values(text: str) -> dict[str, str]:
    """Series name (without labels) -> value of the non-comment lines of a textfile."""
    values = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            series, _, value = line.rpartition(" ")
            values[series.partition("{")[0]] = value
    return values


def _run_script(
    name: str,
    mode: str,
    environment: dict[str, str],
    *,
    stop: tuple[float, signal.Signals] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a backup script; `stop` sends the signal after that many seconds if it still runs."""
    # /bin/sh, not PATH: a local sh wrapper first on PATH would run instead of the system shell.
    command = ["/bin/sh", str(MONITORING / "backup" / name), mode]
    if stop is None:
        return subprocess.run(
            command, env=environment, capture_output=True, text=True, check=False, timeout=timeout
        )
    seconds, sig = stop
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        # The whole group, as GNU timeout does: no stub sleep stays behind with the pipes open.
        os.killpg(process.pid, sig)
        stdout, stderr = process.communicate(timeout=10)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_backup(
    tmp_path: Path,
    *,
    mode: str = "once",
    stop: tuple[float, signal.Signals] | None = None,
    unset: tuple[str, ...] = (),
    **env: str,
) -> BackupRun:
    tools = tmp_path / "pgbin"
    tools.mkdir(exist_ok=True)
    for name, source in FAKE_PG_TOOLS.items():
        (tools / name).write_text(source, encoding="utf-8")
        (tools / name).chmod(0o755)
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    textfile = tmp_path / "pg_backup.prom"
    heartbeat = tmp_path / "pg_backup_heartbeat.prom"
    log = tmp_path / "pg.log"
    environment = {
        "PATH": f"{tools}:{os.environ['PATH']}",
        "PGDATABASE": "app",
        "BACKUP_ENVIRONMENT": "staging",
        "BACKUP_DIR": str(backups),
        "TEXTFILE": str(textfile),
        "HEARTBEAT": str(heartbeat),
        "BACKUP_SNAPSHOT_WAIT_SECONDS": "10",
        "STUB_PG_LOG": str(log),
        "STUB_SOURCE_FINGERPRINT": SOURCE_FINGERPRINT,
        "STUB_RESTORED_FINGERPRINT": SOURCE_FINGERPRINT,
        **env,
    }
    for name in unset:
        del environment[name]
    result = _run_script("pg-backup.sh", mode, environment, stop=stop, timeout=60)
    text = _read(textfile) if textfile.exists() else ""
    calls = _read(log).splitlines() if log.exists() else []
    return BackupRun(
        result,
        backups,
        _metric_values(text),
        calls,
        text,
        _read(heartbeat) if heartbeat.exists() else "",
    )


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


def _seed_dump(directory: Path, name: str, mtime: int) -> Path:
    dump = directory / name
    dump.write_text("dump", encoding="utf-8")
    os.utime(dump, (mtime, mtime))
    return dump


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_backup_metrics_carry_the_environment_label(tmp_path: Path, environment: str) -> None:
    run = _run_backup(tmp_path, BACKUP_ENVIRONMENT=environment)

    series = [line for line in run.textfile.splitlines() if not line.startswith("#")]
    assert run.result.returncode == 0, run.result.stderr
    assert len(series) >= 5
    for line in series:
        assert re.fullmatch(
            rf'dmc_pg_backup_[a-z_]+\{{environment="{environment}"\}}\s+\d+', line
        ), line


@pytest.mark.parametrize("environment", [None, "", "shared", "Prod", "prod "])
def test_backup_refuses_a_missing_or_unknown_environment(
    tmp_path: Path, environment: str | None
) -> None:
    if environment is None:
        run = _run_backup(tmp_path, unset=("BACKUP_ENVIRONMENT",))
    else:
        run = _run_backup(tmp_path, BACKUP_ENVIRONMENT=environment)

    assert run.result.returncode == 2
    assert "BACKUP_ENVIRONMENT" in run.result.stderr
    assert run.calls == []
    assert list(run.backups.iterdir()) == []
    assert run.textfile == ""


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_heartbeat_carries_the_environment_label(tmp_path: Path, environment: str) -> None:
    (tmp_path / "backups").mkdir()
    _seed_dump(tmp_path / "backups", "app-20261103T000000Z.dump", int(time.time()))

    run = _run_backup(
        tmp_path,
        mode="loop",
        stop=(2, signal.SIGTERM),
        BACKUP_ENVIRONMENT=environment,
        BACKUP_POLL_SECONDS="1",
    )

    assert run.result.returncode == 0, run.result.stderr
    assert run.calls == []  # the fresh dump is not due
    heartbeat = [line for line in run.heartbeat.splitlines() if not line.startswith("#")]
    assert len(heartbeat) == 1
    assert re.fullmatch(
        rf'dmc_pg_backup_heartbeat_timestamp_seconds\{{environment="{environment}"\}} \d+',
        heartbeat[0],
    )


def test_an_old_unlabeled_textfile_is_labeled_at_start(tmp_path: Path) -> None:
    (tmp_path / "backups").mkdir()
    _seed_dump(tmp_path / "backups", "app-20261103T000000Z.dump", int(time.time()))
    textfile = tmp_path / "pg_backup.prom"
    textfile.write_text(
        "# HELP dmc_pg_backup_last_run_success 1 if the last backup attempt succeeded.\n"
        "# TYPE dmc_pg_backup_last_run_success gauge\n"
        "dmc_pg_backup_last_run_success 1\n"
        "# HELP dmc_pg_backup_files Dumps kept in the backup directory.\n"
        "# TYPE dmc_pg_backup_files gauge\n"
        "dmc_pg_backup_files 4\n",
        encoding="utf-8",
    )
    stop_after_two_seconds = (2, signal.SIGTERM)

    first = _run_backup(tmp_path, mode="loop", stop=stop_after_two_seconds, BACKUP_POLL_SECONDS="1")
    second = _run_backup(
        tmp_path, mode="loop", stop=stop_after_two_seconds, BACKUP_POLL_SECONDS="1"
    )

    assert first.result.returncode == 0, first.result.stderr
    assert 'dmc_pg_backup_last_run_success{environment="staging"} 1\n' in first.textfile
    assert 'dmc_pg_backup_files{environment="staging"} 4\n' in first.textfile
    assert "# TYPE dmc_pg_backup_files gauge\n" in first.textfile
    assert second.textfile == first.textfile


# --- off-host copy of the dumps ------------------------------------------------------------


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
# A dump finishing while the copy runs: pg-backup renames the .partial file to .dump.
if [ "$1" = copy ] && [ -n "${STUB_PROMOTE_PARTIAL:-}" ]; then
  mv "${STUB_PROMOTE_PARTIAL}" "${STUB_PROMOTE_PARTIAL%.partial}"
fi
# A slow copy: the first pass is still running when the test stops the service.
if [ "$1" = copy ] && [ -n "${STUB_COPY_SLEEP:-}" ]; then sleep "${STUB_COPY_SLEEP}"; fi
if [ "$1" = copy ] && [ -n "${STUB_COPY_FAILS:-}" ]; then
  echo "Failed to copy: 403 Forbidden" >&2
  exit 1
fi
"""


def _run_offsite(
    tmp_path: Path,
    *,
    mode: str = "once",
    stop: tuple[float, signal.Signals] | None = None,
    unset: tuple[str, ...] = (),
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    tools = tmp_path / "rclonebin"
    tools.mkdir(exist_ok=True)
    (tools / "rclone").write_text(FAKE_RCLONE, encoding="utf-8")
    (tools / "rclone").chmod(0o755)
    textfile = tmp_path / "pg_backup_offsite.prom"
    log = tmp_path / "rclone.log"
    environment = {
        "PATH": f"{tools}:{os.environ['PATH']}",
        "BACKUP_ENVIRONMENT": "staging",
        "BACKUP_DIR": str(tmp_path),
        "TEXTFILE": str(textfile),
        "STUB_RCLONE_LOG": str(log),
        **env,
    }
    for name in unset:
        del environment[name]
    result = _run_script("offsite.sh", mode, environment, stop=stop)
    calls = _read(log).splitlines() if log.exists() else []
    return result, calls, _read(textfile) if textfile.exists() else ""


def test_offsite_copies_finished_dumps_then_prunes_old_copies(tmp_path: Path) -> None:
    result, calls, metrics = _run_offsite(tmp_path)

    assert result.returncode == 0, result.stderr
    assert calls == [
        f"rclone copy {tmp_path} secure: --include *.dump --immutable --no-traverse",
        "rclone delete secure: --include *.dump --min-age 30d",
    ]
    assert 'dmc_pg_backup_offsite_last_run_success{environment="staging"} 1\n' in metrics
    assert "dmc_pg_backup_offsite_last_success_timestamp_seconds{" in metrics


def test_failed_offsite_copy_never_deletes_remote_dumps(tmp_path: Path) -> None:
    result, calls, metrics = _run_offsite(tmp_path, STUB_COPY_FAILS="1")

    assert result.returncode == 1
    assert [call.split()[1] for call in calls] == ["copy"]
    assert 'dmc_pg_backup_offsite_last_run_success{environment="staging"} 0\n' in metrics
    assert "last_success_timestamp" not in metrics


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_offsite_metrics_carry_the_environment_label(tmp_path: Path, environment: str) -> None:
    _seed_dump(tmp_path, "app-20261103T000000Z.dump", 1_793_000_000)

    result, _, metrics = _run_offsite(tmp_path, BACKUP_ENVIRONMENT=environment)

    series = [line for line in metrics.splitlines() if not line.startswith("#")]
    assert result.returncode == 0, result.stderr
    assert len(series) == 3
    for line in series:
        assert re.fullmatch(
            rf'dmc_pg_backup_offsite_[a-z_]+\{{environment="{environment}"\}} \d+', line
        ), line


@pytest.mark.parametrize("environment", [None, "", "shared", "Prod"])
def test_offsite_refuses_a_missing_or_unknown_environment(
    tmp_path: Path, environment: str | None
) -> None:
    if environment is None:
        result, calls, metrics = _run_offsite(tmp_path, unset=("BACKUP_ENVIRONMENT",))
    else:
        result, calls, metrics = _run_offsite(tmp_path, BACKUP_ENVIRONMENT=environment)

    assert result.returncode == 2
    assert "BACKUP_ENVIRONMENT" in result.stderr
    assert calls == []
    assert metrics == ""


def test_offsite_records_the_dump_a_successful_pass_covered(tmp_path: Path) -> None:
    _seed_dump(tmp_path, "app-20261101T000000Z.dump", 1_793_000_000)
    _seed_dump(tmp_path, "app-20261102T000000Z.dump", 1_793_086_400)
    key = "dmc_pg_backup_offsite_last_copied_dump_timestamp_seconds"

    result, _, metrics = _run_offsite(tmp_path, BACKUP_ENVIRONMENT="prod")

    assert result.returncode == 0, result.stderr
    assert f'{key}{{environment="prod"}} 1793086400\n' in metrics
    assert f"# TYPE {key} gauge\n" in metrics


def test_offsite_names_the_dump_that_existed_before_the_copy_started(tmp_path: Path) -> None:
    # pg-backup renames a finished .partial file to .dump while rclone runs: that pass may have
    # skipped it, so it must not be claimed as copied.
    _seed_dump(tmp_path, "app-20261101T000000Z.dump", 1_793_000_000)
    partial = _seed_dump(tmp_path, "app-20261102T000000Z.dump.partial", 1_793_086_400)

    result, calls, metrics = _run_offsite(tmp_path, STUB_PROMOTE_PARTIAL=str(partial))

    assert result.returncode == 0, result.stderr
    assert calls[0].startswith("rclone copy")
    assert (tmp_path / "app-20261102T000000Z.dump").exists()
    assert 'last_copied_dump_timestamp_seconds{environment="staging"} 1793000000\n' in metrics


def test_offsite_records_no_dump_after_a_failed_pass_or_without_dumps(tmp_path: Path) -> None:
    key = "last_copied_dump_timestamp_seconds"
    (tmp_path / "empty").mkdir()
    nothing, _, nothing_metrics = _run_offsite(tmp_path / "empty")
    _seed_dump(tmp_path, "app-20261101T000000Z.dump", 1_793_000_000)
    failed, _, failed_metrics = _run_offsite(tmp_path, STUB_COPY_FAILS="1")

    assert nothing.returncode == 0, nothing.stderr
    assert "dmc_pg_backup_offsite_last_run_success" in nothing_metrics
    assert key not in nothing_metrics
    assert failed.returncode == 1
    assert 'dmc_pg_backup_offsite_last_run_success{environment="staging"} 0' in failed_metrics
    assert key not in failed_metrics


def test_both_environments_emit_the_same_help_and_type_lines(tmp_path: Path) -> None:
    # node-exporter merges one metric family written by two textfiles only when HELP and TYPE of
    # both are identical.
    def comments(text: str) -> list[str]:
        return sorted(line for line in text.splitlines() if line.startswith("#"))

    texts = {}
    for environment in ("staging", "prod"):
        directory = tmp_path / environment
        (directory / "backups").mkdir(parents=True)
        _seed_dump(directory / "backups", "app-20261103T000000Z.dump", int(time.time()) - 90000)
        backup = _run_backup(directory, BACKUP_ENVIRONMENT=environment)
        _seed_dump(directory, "app-20261103T000000Z.dump", 1_793_000_000)
        _, _, offsite = _run_offsite(directory, BACKUP_ENVIRONMENT=environment)
        texts[environment] = (comments(backup.textfile), comments(offsite))

    assert texts["staging"][0] == texts["prod"][0]
    assert texts["staging"][1] == texts["prod"][1]
    assert len(texts["prod"][0]) == 12  # six families
    assert len(texts["prod"][1]) == 6  # three families


def test_an_old_unlabeled_offsite_textfile_is_labeled_before_the_first_pass(
    tmp_path: Path,
) -> None:
    # Until the first pass of the recreated service ends, the old file would sit beside the
    # labeled one of the prod copy: one metric family with two label sets.
    textfile = tmp_path / "pg_backup_offsite.prom"
    text = (
        "# HELP dmc_pg_backup_offsite_last_run_success 1 if the last copy pass succeeded.\n"
        "# TYPE dmc_pg_backup_offsite_last_run_success gauge\n"
        "dmc_pg_backup_offsite_last_run_success 1\n"
        "# HELP dmc_pg_backup_offsite_last_success_timestamp_seconds Time of the last pass.\n"
        "# TYPE dmc_pg_backup_offsite_last_success_timestamp_seconds gauge\n"
        "dmc_pg_backup_offsite_last_success_timestamp_seconds 1793000000\n"
    )
    textfile.write_text(text, encoding="utf-8")
    # SIGKILL while the slow first pass runs: only the start-up relabel can have touched the file.
    stop = (2, signal.SIGKILL)

    _, _, first = _run_offsite(tmp_path, mode="loop", stop=stop, STUB_COPY_SLEEP="3")
    _, _, second = _run_offsite(tmp_path, mode="loop", stop=stop, STUB_COPY_SLEEP="3")

    assert 'dmc_pg_backup_offsite_last_run_success{environment="staging"} 1\n' in first
    assert (
        'dmc_pg_backup_offsite_last_success_timestamp_seconds{environment="staging"} 1793000000\n'
        in first
    )
    assert "# TYPE dmc_pg_backup_offsite_last_run_success gauge\n" in first
    assert second == first
