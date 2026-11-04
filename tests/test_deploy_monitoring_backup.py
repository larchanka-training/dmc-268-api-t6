"""pg-backup.sh and the off-host copy of the dumps of the monitoring stack."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.monitoring_deploy_host import MONITORING, Host, _job, _read

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
